"""Execution-independent strategy contract and immutable, closed-only market views."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from trending_basket.domain.types import Candle, Interval


class MarketView:
    """Owns only already-closed candles, never a reference to the future data store."""

    __slots__ = ("_closed", "time_ms")

    def __init__(
        self, time_ms: int, closed: Mapping[tuple[str, Interval], tuple[Candle, ...]]
    ) -> None:
        # DataStore validates chronological order once; each tuple here is an owned prefix.
        if any(
            rows and rows[-1].open_time_ms + rows[-1].interval.duration_ms > time_ms
            for rows in closed.values()
        ):
            raise ValueError("MarketView cannot contain an unclosed candle")
        self.time_ms = time_ms
        self._closed = MappingProxyType(dict(closed))

    def candles(self, symbol: str, interval: Interval, lookback: int) -> tuple[Candle, ...]:
        if lookback <= 0:
            raise ValueError("lookback must be positive")
        return self._closed.get((symbol, interval), ())[-lookback:]


@dataclass(frozen=True, slots=True)
class PositionView:
    quantity: float
    average_entry_price: float
    stop_price: float | None
    initial_risk_usd: float | None
    notional_usd: float


@dataclass(frozen=True, slots=True)
class DecisionContext:
    time_ms: int
    equity_usd: float
    positions: Mapping[str, PositionView]
    universe: tuple[str, ...]
    market: MarketView


@dataclass(frozen=True, slots=True)
class TargetPosition:
    notional_usd: float
    stop_price: float | None = None
    initial_risk_usd: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.notional_usd):
            raise ValueError("target notional must be finite")
        for value in (self.stop_price, self.initial_risk_usd):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("stop and initial risk must be positive and finite")


Decision = dict[str, TargetPosition]


class Strategy(Protocol):
    name: str

    def decide(self, ctx: DecisionContext) -> Decision: ...
