"""Core domain value types: candles, intervals, funding rates, weights, sides."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Interval(StrEnum):
    """A candle timeframe."""

    H4 = "4h"
    D1 = "1d"

    def to_bybit(self) -> str:
        """Return the Bybit REST API kline interval code for this timeframe."""
        return {Interval.H4: "240", Interval.D1: "D"}[self]

    @property
    def duration_ms(self) -> int:
        """Return the timeframe's duration in milliseconds."""
        return {Interval.H4: 4 * 60 * 60 * 1000, Interval.D1: 24 * 60 * 60 * 1000}[self]


class Side(StrEnum):
    """A position or order direction."""

    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV candle for a symbol and interval."""

    symbol: str
    interval: Interval
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float

    def __post_init__(self) -> None:
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise ValueError("candle prices must be positive")
        if self.high < max(self.open, self.close):
            raise ValueError("high must be >= max(open, close)")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= min(open, close)")
        if self.volume < 0:
            raise ValueError("volume must be >= 0")
        if self.open_time_ms % self.interval.duration_ms != 0:
            raise ValueError(
                f"open_time_ms={self.open_time_ms} is not aligned to "
                f"{self.interval} ({self.interval.duration_ms} ms)"
            )


@dataclass(frozen=True, slots=True)
class FundingRate:
    """A single funding rate observation for a perpetual symbol."""

    symbol: str
    funding_time_ms: int
    rate_frac: float


@dataclass(frozen=True, slots=True)
class TargetWeight:
    """A target portfolio weight for a symbol, as a fraction of equity."""

    symbol: str
    weight_frac: float

    def __post_init__(self) -> None:
        if not -1 <= self.weight_frac <= 1:
            raise ValueError(f"weight_frac must be in [-1, 1], got {self.weight_frac}")
