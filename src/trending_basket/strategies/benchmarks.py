"""Reference strategies, without trend signals or parameter fitting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trending_basket.domain.types import Interval
from trending_basket.strategies.base import Decision, DecisionContext, TargetPosition
from trending_basket.strategies.trend_basket import TrendBasket, TrendBasketParams
from trending_basket.universe.selection import month_start_ms


def hold_positions(ctx: DecisionContext) -> Decision:
    return {
        symbol: TargetPosition(p.notional_usd, p.stop_price, p.initial_risk_usd)
        for symbol, p in ctx.positions.items()
    }


@dataclass
class BuyAndHoldBTC:
    name: str = "buy_and_hold_btc"
    entered: bool = False

    def decide(self, ctx: DecisionContext) -> Decision:
        if self.entered:
            return hold_positions(ctx)
        if "BTCUSDT" not in ctx.universe:
            return {}
        self.entered = True
        return {"BTCUSDT": TargetPosition(max(0.0, ctx.equity_usd))}


@dataclass
class EqualWeightUniverse:
    name: str = "equal_weight_universe"
    last_month_ms: int | None = None

    def decide(self, ctx: DecisionContext) -> Decision:
        month = month_start_ms(ctx.time_ms)
        if month == self.last_month_ms:
            return hold_positions(ctx)
        self.last_month_ms = month
        weight = max(0.0, ctx.equity_usd) / len(ctx.universe) if ctx.universe else 0.0
        return {symbol: TargetPosition(weight) for symbol in ctx.universe}


def make_strategy(
    name: str, params: dict[str, Any] | None = None, interval: Interval = Interval.D1
) -> BuyAndHoldBTC | EqualWeightUniverse | TrendBasket:
    if name == "trend_basket":
        return TrendBasket(TrendBasketParams.model_validate(params or {}), interval)
    if name == "buy_and_hold_btc":
        return BuyAndHoldBTC()
    if name == "equal_weight_universe":
        return EqualWeightUniverse()
    raise ValueError(f"unknown strategy: {name}")
