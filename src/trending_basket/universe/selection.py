"""Deterministic universe selection using only candles closed by rebalance time."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from trending_basket.domain.types import Interval

DAY_MS = Interval.D1.duration_ms
UNIVERSE_DTYPES = {
    "rebalance_time_ms": "int64",
    "symbol": "str",
    "rank": "int64",
    "median_turnover_usd": "float64",
    "history_days": "int64",
}


@dataclass(frozen=True)
class SelectionParameters:
    top_n: int = 15
    min_history_days: int = 120
    turnover_window_days: int = 30
    min_median_turnover_usd: float = 10_000_000.0

    def __post_init__(self) -> None:
        for value in (self.top_n, self.min_history_days, self.turnover_window_days):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(
                    "top_n, min_history_days and turnover_window_days must be positive integers"
                )
        if not math.isfinite(self.min_median_turnover_usd) or self.min_median_turnover_usd < 0:
            raise ValueError("min_median_turnover_usd must be finite and nonnegative")


def empty_universe() -> pd.DataFrame:
    return pd.DataFrame({key: pd.Series(dtype=value) for key, value in UNIVERSE_DTYPES.items()})


def month_start_ms(time_ms: int) -> int:
    value = datetime.fromtimestamp(time_ms / 1000, tz=UTC)
    return int(value.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def monthly_schedule(since_ms: int, until_ms: int) -> list[int]:
    if month_start_ms(since_ms) != since_ms:
        raise ValueError("since must be the first day of a month at 00:00 UTC")
    if since_ms > until_ms:
        raise ValueError("since must not be in the future")
    current = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
    last = month_start_ms(until_ms)
    result = []
    while int(current.timestamp() * 1000) <= last:
        result.append(int(current.timestamp() * 1000))
        current = (
            current.replace(year=current.year + 1, month=1)
            if current.month == 12
            else current.replace(month=current.month + 1)
        )
    return result


def select_universe(
    candles: Mapping[str, pd.DataFrame],
    rebalance_time_ms: int,
    parameters: SelectionParameters,
) -> pd.DataFrame:
    if month_start_ms(rebalance_time_ms) != rebalance_time_ms:
        raise ValueError("rebalance time must be a month boundary at 00:00 UTC")
    eligible: list[tuple[str, float, int]] = []
    window_start_ms = rebalance_time_ms - parameters.turnover_window_days * DAY_MS
    expected_window = list(range(window_start_ms, rebalance_time_ms, DAY_MS))
    for symbol, frame in candles.items():
        # Slice first: future turnover, gaps and duplicates cannot influence eligibility.
        closed = frame.loc[
            frame["open_time_ms"] <= rebalance_time_ms - DAY_MS, ["open_time_ms", "turnover"]
        ]
        if len(closed) < parameters.min_history_days:
            continue
        times = closed["open_time_ms"]
        if times.duplicated().any() or (times % DAY_MS != 0).any():
            continue
        turnover = closed["turnover"]
        if not turnover.map(math.isfinite).all() or (turnover < 0).any():
            continue
        window = closed.loc[times >= window_start_ms].sort_values("open_time_ms")
        if window["open_time_ms"].tolist() != expected_window:
            continue
        median = float(window["turnover"].median())
        if median >= parameters.min_median_turnover_usd:
            eligible.append((symbol, median, len(closed)))
    eligible.sort(key=lambda item: (-item[1], item[0]))
    rows = [
        {
            "rebalance_time_ms": rebalance_time_ms,
            "symbol": symbol,
            "rank": rank,
            "median_turnover_usd": median,
            "history_days": history_days,
        }
        for rank, (symbol, median, history_days) in enumerate(eligible[: parameters.top_n], start=1)
    ]
    return pd.DataFrame(rows).astype(UNIVERSE_DTYPES) if rows else empty_universe()
