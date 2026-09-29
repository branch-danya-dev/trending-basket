"""Delisting bounds, no future delisting filter, and offline artifact integration."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner
from universe_test_support import JUNE_MS, MAY_MS, instrument, seed_universe_inputs

from trending_basket.cli import app
from trending_basket.clock import ManualClock
from trending_basket.data.cache import klines_path
from trending_basket.domain.types import Interval
from trending_basket.universe.lifecycle import trading_period
from trending_basket.universe.selection import DAY_MS, SelectionParameters, select_universe
from trending_basket.universe.storage import build_universe, load_universe, universe_paths


def history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time_ms": list(range(MAY_MS - 150 * DAY_MS, MAY_MS + 10 * DAY_MS, DAY_MS)),
            "turnover": [20_000_000.0] * 160,
        }
    )


@pytest.mark.parametrize(
    "offset,eligible", [(-1, False), (0, False), (1, True), (30 * DAY_MS, True)]
)
def test_delisting_boundary_at_rebalance(offset: int, eligible: bool) -> None:
    frame = history()
    period = trading_period("Closed", MAY_MS + offset, frame)
    actual = select_universe({"OLDUSDT": frame}, MAY_MS, SelectionParameters(), {"OLDUSDT": period})
    assert actual["symbol"].tolist() == (["OLDUSDT"] if eligible else [])


def test_future_delisting_date_and_current_status_do_not_change_past_selection() -> None:
    frames = {"OLDUSDT": history(), "BTCUSDT": history()}
    frames["OLDUSDT"]["turnover"] = 30_000_000.0
    baseline = select_universe(frames, MAY_MS, SelectionParameters(top_n=1))
    assert baseline["symbol"].tolist() == ["OLDUSDT"]
    for status, until in [
        ("Trading", None),
        ("Closed", MAY_MS + 1),
        ("Closed", JUNE_MS + 20 * DAY_MS),
    ]:
        periods = {
            "OLDUSDT": trading_period(status, until, frames["OLDUSDT"]),
            "BTCUSDT": trading_period("Trading", None, frames["BTCUSDT"]),
        }
        actual = select_universe(frames, MAY_MS, SelectionParameters(top_n=1), periods)
        pd.testing.assert_frame_equal(actual, baseline)


@pytest.mark.parametrize("value", [None, 0])
def test_missing_snapshot_end_falls_back_to_last_candle_close(value: int | None) -> None:
    period = trading_period("Closed", value, history())
    assert period.listed_until_ms == MAY_MS + 10 * DAY_MS
    assert period.listed_until_source == "last_candle_close"


def test_snapshot_end_has_precedence_and_trading_has_no_end() -> None:
    period = trading_period("Closed", MAY_MS + 12345, history())
    assert period.listed_until_ms == MAY_MS + 12345
    assert period.listed_until_source == "snapshot_delivery_time"
    active = trading_period("Trading", MAY_MS + 12345, history())
    assert active.listed_until_ms is None
    assert active.contains(JUNE_MS)


def test_delisted_build_roundtrip_missing_history_report_and_repeatability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    snapshot = tmp_path / "bybit/linear/instruments/2024-06-01.parquet"
    rows = [
        instrument(symbol, status="Closed")
        for symbol in ["BTCUSDT", "ETHUSDT", "MISSINGUSDT", "EMPTYUSDT"]
    ]
    frame = pd.DataFrame(rows)
    frame["delivery_time_ms"] = [MAY_MS + 15 * DAY_MS, 0, JUNE_MS, 0]
    frame.to_parquet(snapshot, index=False)
    history().to_parquet(klines_path(tmp_path, Interval.D1, "ETHUSDT"), index=False)
    history().iloc[:0].to_parquet(klines_path(tmp_path, Interval.D1, "EMPTYUSDT"), index=False)
    kwargs = dict(
        data_dir=tmp_path,
        name="core15",
        since_ms=MAY_MS,
        parameters=SelectionParameters(),
        clock=ManualClock(JUNE_MS),
        exclusions_path=exclusions,
    )
    built = build_universe(**kwargs)
    assert built.universe_at(MAY_MS) == ["BTCUSDT", "ETHUSDT"]
    assert built.universe_at(JUNE_MS) == []
    parquet, _ = universe_paths(tmp_path, "core15")
    before = parquet.read_bytes()
    build_universe(**kwargs)
    assert parquet.read_bytes() == before
    result = load_universe(tmp_path, "core15")
    first = MAY_MS - 120 * DAY_MS
    end = MAY_MS + 15 * DAY_MS
    assert not result.is_tradeable_at("BTCUSDT", first - 1)
    assert result.is_tradeable_at("BTCUSDT", first)
    assert result.is_tradeable_at("BTCUSDT", end - 1)
    assert not result.is_tradeable_at("BTCUSDT", end)
    assert not result.is_tradeable_at("BTCUSDT", end + 1)
    assert "BTCUSDT" in result.universe_at(end)  # Monthly membership is not a trading permission.
    assert not result.is_tradeable_at("UNKNOWN", MAY_MS)
    assert not result.is_tradeable_at("MISSINGUSDT", MAY_MS)
    assert not result.is_tradeable_at("EMPTYUSDT", MAY_MS)
    assert result.metadata["survivorship_bias"]["delisted_included"] == 2
    assert result.metadata["survivorship_bias"]["delisted_without_history"] == 2
    assert result.metadata["delisted_without_history_symbols"] == ["EMPTYUSDT", "MISSINGUSDT"]
    assert result.table["listed_until_source"].tolist() == [
        "snapshot_delivery_time",
        "last_candle_close",
    ]
    assert str(result.table["listed_until_ms"].dtype) == "Int64"
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    report = CliRunner().invoke(app, ["universe", "report", "core15"])
    assert report.exit_code == 0, report.output
    assert "later_closed" in report.output
    assert "underfilled | BTCUSDT,ETHUSDT" in report.output
    assert "delisted without history: EMPTYUSDT,MISSINGUSDT" in report.output
