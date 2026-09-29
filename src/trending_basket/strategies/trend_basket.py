"""Preregistered channel states, daily Wilder ATR and monotone trailing stops."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trending_basket.domain.types import Candle, Interval
from trending_basket.strategies.base import Decision, DecisionContext, TargetPosition


class TrendBasketParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    lookbacks_days: tuple[int, ...] = (20, 55, 100)
    exit_ratio: float = Field(default=0.5, gt=0, le=1)
    atr_days: int = Field(default=20, ge=1)
    stop_atr_mult: float = Field(default=3.0, gt=0)
    risk_per_symbol_frac: float = Field(default=0.005, gt=0, le=1)
    rebalance_band_frac: float = Field(default=0.25, ge=0)
    direction: Literal["long_only", "long_short"] = "long_only"

    @field_validator("lookbacks_days")
    @classmethod
    def positive_unique(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if not values or any(v <= 0 for v in values) or len(set(values)) != len(values):
            raise ValueError("lookbacks must be positive and unique")
        return values


@dataclass
class SymbolState:
    subsystems: list[int]
    last_close_ms: int = -1
    stopped_at_ms: int = -1
    lifecycle_id: int | None = None
    extreme_close: float | None = None
    atr_time_ms: int = -1
    atr: float | None = None


@dataclass
class TrendBasket:
    params: TrendBasketParams = field(default_factory=TrendBasketParams)
    interval: Interval = Interval.D1
    name: str = field(default="trend_basket", init=False)
    states: dict[str, SymbolState] = field(default_factory=dict, init=False)

    def _atr(self, state: SymbolState, daily: tuple[Candle, ...]) -> float | None:
        period = self.params.atr_days
        if len(daily) < period + 1:
            return None
        if state.atr_time_ms == daily[-1].open_time_ms:
            return state.atr
        ranges = []
        for previous, bar in pairwise(daily):
            if bar.open_time_ms <= state.atr_time_ms:
                continue
            tr = max(
                bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close)
            )
            if state.atr is None:
                ranges.append(tr)
                if len(ranges) == period:
                    state.atr = sum(ranges) / period
            else:
                state.atr = ((period - 1) * state.atr + tr) / period
            if state.atr is not None:
                state.atr_time_ms = bar.open_time_ms
        return state.atr

    def decide(self, ctx: DecisionContext) -> Decision:
        for event in ctx.recent_exits:
            state = self.states.get(event.symbol)
            if state is not None and event.reason == "stop":
                state.subsystems = [0] * len(self.params.lookbacks_days)
                state.stopped_at_ms = max(state.stopped_at_ms, event.time_ms)
                state.lifecycle_id = None
                state.extreme_close = None
        decision: Decision = {}
        multiplier = 6 if self.interval == Interval.H4 else 1
        lengths = [days * multiplier for days in self.params.lookbacks_days]
        for symbol in sorted(set(ctx.universe) | set(ctx.positions)):
            state = self.states.setdefault(symbol, SymbolState([0] * len(lengths)))
            position = ctx.positions.get(symbol)
            if position:
                decision[symbol] = TargetPosition(
                    position.notional_usd, position.stop_price, position.initial_risk_usd
                )
            bars = ctx.market.candles(symbol, self.interval, max(lengths) + 1)
            daily = ctx.market.candles(symbol, Interval.D1, 1_000_000)
            atr = self._atr(state, daily)
            if len(bars) < max(lengths) + 1 or atr is None or atr <= 0:
                continue
            close_ms = bars[-1].open_time_ms + self.interval.duration_ms
            if close_ms <= state.last_close_ms:
                continue
            state.last_close_ms = close_ms
            close = bars[-1].close
            old_signal = sum(state.subsystems) / len(lengths)
            if close_ms > state.stopped_at_ms:
                for i, length in enumerate(lengths):
                    previous = bars[-length - 1 : -1]
                    exit_length = max(1, math.floor(length * self.params.exit_ratio))
                    exit_bars = bars[-exit_length - 1 : -1]
                    value = state.subsystems[i]
                    if (value > 0 and close < min(b.low for b in exit_bars)) or (
                        value < 0 and close > max(b.high for b in exit_bars)
                    ):
                        value = 0
                    if value == 0:
                        if close > max(b.high for b in previous):
                            value = 1
                        elif self.params.direction == "long_short" and close < min(
                            b.low for b in previous
                        ):
                            value = -1
                    state.subsystems[i] = value
            signal = sum(state.subsystems) / len(lengths)
            if signal == 0 or ctx.equity_usd <= 0:
                decision[symbol] = TargetPosition(0)
                continue
            current = position.notional_usd if position else 0.0
            atr_frac = atr / close
            target = (
                signal
                * self.params.risk_per_symbol_frac
                * ctx.equity_usd
                / (self.params.stop_atr_mult * atr_frac)
            )
            if (
                signal == old_signal
                and current
                and abs(target - current) / abs(current) <= self.params.rebalance_band_frac
            ):
                target = current
            if symbol not in ctx.universe:
                target = (
                    math.copysign(min(abs(target), abs(current)), current)
                    if target * current > 0
                    else 0.0
                )
            if target == 0:
                decision[symbol] = TargetPosition(0)
                continue
            same_position = position is not None and position.quantity * target > 0
            long = target > 0
            if same_position:
                assert position is not None
                if state.lifecycle_id != position.lifecycle_id:
                    state.lifecycle_id = position.lifecycle_id
                    state.extreme_close = None
                # The entry-boundary decision candle closed before the position existed.
                if close_ms > position.entry_time_ms:
                    state.extreme_close = (
                        close
                        if state.extreme_close is None
                        else (
                            max(state.extreme_close, close)
                            if long
                            else min(state.extreme_close, close)
                        )
                    )
            else:
                state.lifecycle_id = None
                state.extreme_close = None
            extreme = (
                state.extreme_close if same_position and state.extreme_close is not None else close
            )
            stop = extreme - math.copysign(self.params.stop_atr_mult * atr, target)
            if same_position and position is not None and position.stop_price is not None:
                stop = max(stop, position.stop_price) if long else min(stop, position.stop_price)
            # A nonpositive long stop cannot be expressed as a valid exchange price.
            # Keep the intended wide distance; no touch is possible below zero.
            stop = max(stop, float.fromhex("0x1.0p-1022"))
            risk = abs(target) * self.params.stop_atr_mult * atr_frac
            decision[symbol] = TargetPosition(target, stop, risk)
        return decision
