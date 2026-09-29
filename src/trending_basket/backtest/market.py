"""Indexed immutable market data, separate from the strategy's closed-only view."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence

from trending_basket.domain.types import Candle, Interval
from trending_basket.strategies.base import MarketView


class DataStore:
    def __init__(self, candles: Mapping[tuple[str, Interval], Sequence[Candle]]) -> None:
        self.rows = {
            key: tuple(sorted(values, key=lambda c: c.open_time_ms))
            for key, values in candles.items()
        }
        self.times = {
            key: tuple(c.open_time_ms for c in values) for key, values in self.rows.items()
        }
        for key, times in self.times.items():
            if len(set(times)) != len(times):
                raise ValueError(f"duplicate candle timestamps: {key}")

    def bar(self, symbol: str, interval: Interval, at_ms: int) -> Candle | None:
        key = symbol, interval
        times = self.times.get(key, ())
        index = bisect_left(times, at_ms)
        return self.rows[key][index] if index < len(times) and times[index] == at_ms else None

    def last_closed(self, symbol: str, interval: Interval, at_ms: int) -> Candle | None:
        key = symbol, interval
        index = bisect_right(self.times.get(key, ()), at_ms - interval.duration_ms) - 1
        return self.rows[key][index] if index >= 0 else None

    def close_price(self, symbol: str, at_ms: int) -> float:
        available = [
            c
            for interval in Interval
            if (c := self.last_closed(symbol, interval, at_ms)) is not None
        ]
        if not available:
            raise ValueError(f"no closed price for {symbol} at {at_ms}")
        return max(available, key=lambda c: c.open_time_ms + c.interval.duration_ms).close

    def funding_price(self, symbol: str, at_ms: int) -> tuple[float, str]:
        bar = self.bar(symbol, Interval.H4, at_ms)
        if bar is not None:
            return bar.open, "4h_open"
        bar = self.bar(
            symbol, Interval.D1, at_ms // Interval.D1.duration_ms * Interval.D1.duration_ms
        )
        if bar is None:
            raise ValueError(f"no funding price for {symbol} at {at_ms}")
        return bar.open, "1d_open_fallback"

    def view(self, at_ms: int, symbols: Sequence[str]) -> MarketView:
        closed = {}
        for symbol in symbols:
            for interval in Interval:
                key = symbol, interval
                end = bisect_right(self.times.get(key, ()), at_ms - interval.duration_ms)
                if end:
                    closed[key] = self.rows[key][:end]
        return MarketView(at_ms, closed)
