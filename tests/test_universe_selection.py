"""Point-in-time selection, eligibility and deterministic ranking."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from trending_basket.universe.selection import (
    DAY_MS,
    SelectionParameters,
    monthly_schedule,
    select_universe,
)

REBALANCE_MS = int(datetime(2024, 5, 1, tzinfo=UTC).timestamp() * 1000)


def history(
    days: int = 120, turnover: float = 20_000_000, until_ms: int = REBALANCE_MS
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time_ms": list(range(until_ms - days * DAY_MS, until_ms, DAY_MS)),
            "turnover": [turnover] * days,
        }
    )


def test_selection_ignores_changes_appends_and_invalid_rows_in_future() -> None:
    past = history()
    baseline = select_universe({"BTCUSDT": past}, REBALANCE_MS, SelectionParameters())
    for future in (
        history(10, 1e15, REBALANCE_MS + 10 * DAY_MS),
        pd.DataFrame(
            {
                "open_time_ms": [REBALANCE_MS, REBALANCE_MS, REBALANCE_MS + 100 * DAY_MS],
                "turnover": [float("nan"), float("inf"), -1],
            }
        ),
    ):
        changed = pd.concat([past, future], ignore_index=True)
        actual = select_universe({"BTCUSDT": changed}, REBALANCE_MS, SelectionParameters())
        pd.testing.assert_frame_equal(actual, baseline)
    assert baseline.iloc[0]["history_days"] == 120


def test_candle_closing_exactly_at_rebalance_counts_but_opening_there_does_not() -> None:
    candles = history(119)
    candles.loc[len(candles)] = [REBALANCE_MS, 1e15]
    assert select_universe({"BTCUSDT": candles}, REBALANCE_MS, SelectionParameters()).empty
    result = select_universe({"BTCUSDT": history(120)}, REBALANCE_MS, SelectionParameters())
    assert result["symbol"].tolist() == ["BTCUSDT"]
    assert result.iloc[0]["median_turnover_usd"] == 20_000_000


@pytest.mark.parametrize(
    "change", ["short", "gap", "missing_last", "low", "duplicate", "nan", "negative", "unaligned"]
)
def test_ineligible_histories_are_excluded(change: str) -> None:
    frame = history(150)
    if change == "short":
        frame = frame.tail(119)
    elif change == "gap":
        frame = frame.drop(frame.index[-15])
    elif change == "missing_last":
        frame = frame.iloc[:-1]
    elif change == "low":
        frame["turnover"] = 9_999_999
    elif change == "duplicate":
        frame = pd.concat([frame, frame.tail(1)])
    elif change == "nan":
        frame.loc[frame.index[-1], "turnover"] = float("nan")
    elif change == "negative":
        frame.loc[frame.index[-1], "turnover"] = -1
    else:
        frame.loc[frame.index[-1], "open_time_ms"] -= 1
    assert select_universe({"BADUSDT": frame}, REBALANCE_MS, SelectionParameters()).empty


def test_median_resists_spikes_and_ties_sort_by_symbol() -> None:
    spike = history(turnover=10_000_000)
    spike.loc[spike.index[-1], "turnover"] = 1e15
    result = select_universe(
        {"ZUSDT": history(), "AUSDT": history(), "SPIKEUSDT": spike},
        REBALANCE_MS,
        SelectionParameters(top_n=2),
    )
    assert result["symbol"].tolist() == ["AUSDT", "ZUSDT"]
    assert result["rank"].tolist() == [1, 2]


def test_window_boundary_and_threshold_are_inclusive() -> None:
    frame = history(turnover=10_000_000)
    frame.loc[frame.index[-31], "turnover"] = 1e15
    result = select_universe({"BTCUSDT": frame}, REBALANCE_MS, SelectionParameters())
    assert result["median_turnover_usd"].tolist() == [10_000_000]
    assert len(result) == 1


def test_old_gap_does_not_replace_required_observed_history_days() -> None:
    frame = history(121).drop(0)
    assert len(select_universe({"BTCUSDT": frame}, REBALANCE_MS, SelectionParameters())) == 1
    frame = history(120).drop(0)
    assert select_universe({"BTCUSDT": frame}, REBALANCE_MS, SelectionParameters()).empty


def test_monthly_schedule_uses_utc_and_includes_empty_first_month() -> None:
    start = int(datetime(2023, 12, 1, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2024, 2, 19, tzinfo=UTC).timestamp() * 1000)
    assert monthly_schedule(start, end) == [
        int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)
        for year, month in [(2023, 12), (2024, 1), (2024, 2)]
    ]
    with pytest.raises(ValueError):
        monthly_schedule(start + DAY_MS, end)
    with pytest.raises(ValueError):
        monthly_schedule(end, start)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_n": 0},
        {"min_history_days": 0},
        {"turnover_window_days": 0},
        {"min_median_turnover_usd": -1},
        {"min_median_turnover_usd": float("nan")},
    ],
)
def test_invalid_parameters_are_rejected(kwargs: dict[str, int | float]) -> None:
    with pytest.raises(ValueError):
        SelectionParameters(**kwargs)  # type: ignore[arg-type]
