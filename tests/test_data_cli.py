"""Tests for the data CLI: sync klines, check, show."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from bybit_test_support import FakeBybitClient, make_candle
from typer.testing import CliRunner

from trending_basket.cli import app
from trending_basket.data import cli as data_cli
from trending_basket.data.cache import klines_path
from trending_basket.domain.types import Interval

runner = CliRunner()

DAY_MS = Interval.D1.duration_ms
BASE_MS = 1704067200000  # 2024-01-01T00:00:00Z


def test_sync_klines_cli_reports_added_rows_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    candles = [make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(3)]
    fake_client = FakeBybitClient(server_now_ms=BASE_MS + 3 * DAY_MS, all_candles=candles)
    monkeypatch.setattr(data_cli, "get_client", lambda: fake_client)

    result = runner.invoke(
        app,
        [
            "data",
            "sync",
            "klines",
            "--symbols",
            "BTCUSDT",
            "--interval",
            "1d",
            "--since",
            "2024-01-01",
        ],
    )

    assert result.exit_code == 0
    assert "BTCUSDT: +3 rows" in result.output
    assert klines_path(tmp_path, Interval.D1, "BTCUSDT").is_file()


def test_sync_klines_cli_continues_past_symbol_error_but_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    candles = [make_candle("ETHUSDT", BASE_MS)]
    fake_client = FakeBybitClient(
        server_now_ms=BASE_MS + DAY_MS, all_candles=candles, fail_symbols=frozenset({"BTCUSDT"})
    )
    monkeypatch.setattr(data_cli, "get_client", lambda: fake_client)

    result = runner.invoke(
        app,
        [
            "data",
            "sync",
            "klines",
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--interval",
            "1d",
            "--since",
            "2024-01-01",
        ],
    )

    assert result.exit_code == 1
    assert "BTCUSDT: ERROR" in result.output
    assert "ETHUSDT: +1 rows" in result.output
    assert klines_path(tmp_path, Interval.D1, "ETHUSDT").is_file()
    assert not klines_path(tmp_path, Interval.D1, "BTCUSDT").is_file()


def _write_klines(path: Path, open_times_ms: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "open_time_ms": open_times_ms,
            "open": [100.0] * len(open_times_ms),
            "high": [101.0] * len(open_times_ms),
            "low": [99.0] * len(open_times_ms),
            "close": [100.0] * len(open_times_ms),
            "volume": [1.0] * len(open_times_ms),
            "turnover": [100.0] * len(open_times_ms),
        }
    ).to_parquet(path, index=False)


def test_check_cli_reports_gaps_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    _write_klines(klines_path(tmp_path, Interval.D1, "BTCUSDT"), [BASE_MS, BASE_MS + 2 * DAY_MS])

    result = runner.invoke(app, ["data", "check", "--interval", "1d"])

    assert result.exit_code == 1
    assert "gaps=1" in result.output


def test_check_cli_exits_zero_when_cache_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    _write_klines(
        klines_path(tmp_path, Interval.D1, "BTCUSDT"),
        [BASE_MS, BASE_MS + DAY_MS, BASE_MS + 2 * DAY_MS],
    )

    result = runner.invoke(app, ["data", "check", "--interval", "1d"])

    assert result.exit_code == 0
    assert "gaps=0" in result.output
    assert "invalid=0" in result.output


def test_show_cli_prints_tail_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    _write_klines(
        klines_path(tmp_path, Interval.D1, "BTCUSDT"),
        [BASE_MS + i * DAY_MS for i in range(5)],
    )

    result = runner.invoke(app, ["data", "show", "BTCUSDT", "--interval", "1d", "--tail", "2"])

    assert result.exit_code == 0
    assert str(BASE_MS + 3 * DAY_MS) in result.output
    assert str(BASE_MS + 4 * DAY_MS) in result.output
    assert str(BASE_MS) not in result.output


def test_show_cli_missing_symbol_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))

    result = runner.invoke(app, ["data", "show", "NOSUCHSYM", "--interval", "1d"])

    assert result.exit_code == 1
