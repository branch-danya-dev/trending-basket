"""Observed trading bounds; future closure dates are never ranking inputs."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trending_basket.domain.types import Interval


@dataclass(frozen=True)
class TradingPeriod:
    listed_from_ms: int | None
    listed_until_ms: int | None
    listed_until_source: str
    status: str

    def contains(self, time_ms: int) -> bool:
        return (
            self.listed_from_ms is not None
            and self.listed_from_ms <= time_ms
            and (self.listed_until_ms is None or time_ms < self.listed_until_ms)
        )


def trading_period(
    status: str, delivery_time_ms: int | None, candles: pd.DataFrame | None
) -> TradingPeriod:
    """Use snapshot delisting time, otherwise the last daily candle's close as an estimate."""
    first = (
        int(candles["open_time_ms"].min()) if candles is not None and not candles.empty else None
    )
    if status == "Trading":
        return TradingPeriod(first, None, "open", status)
    if status != "Closed":
        raise ValueError(f"unsupported candidate status: {status}")
    if delivery_time_ms is not None and delivery_time_ms > 0:
        return TradingPeriod(first, delivery_time_ms, "snapshot_delivery_time", status)
    if candles is not None and not candles.empty:
        last_close = int(candles["open_time_ms"].max()) + Interval.D1.duration_ms
        return TradingPeriod(first, last_close, "last_candle_close", status)
    return TradingPeriod(None, None, "unavailable", status)
