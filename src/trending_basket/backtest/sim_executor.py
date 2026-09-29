"""Deterministic fills, instrument rounding and an independent cash/PnL ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from trending_basket.backtest.config import Costs
from trending_basket.strategies.base import PositionView, TargetPosition


@dataclass(frozen=True)
class InstrumentRules:
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional_value: Decimal | None
    funding_interval_ms: int

    def __post_init__(self) -> None:
        if self.qty_step <= 0 or self.min_order_qty < 0 or self.funding_interval_ms <= 0:
            raise ValueError("invalid instrument rules")

    def quantity(self, notional_usd: float, price: float) -> float:
        raw = Decimal(str(abs(notional_usd))) / Decimal(str(price))
        rounded = (raw / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step
        return float(rounded)


@dataclass
class Position:
    lifecycle_id: int
    symbol: str
    quantity: float
    average_entry_price: float
    average_reference_price: float
    entry_time_ms: int
    stop_price: float | None
    initial_risk_usd: float | None
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    funding_usd: float = 0.0


class SimExecutor:
    def __init__(self, capital_usd: float, rules: dict[str, InstrumentRules], costs: Costs) -> None:
        self.initial_capital_usd = capital_usd
        self.cash_usd = capital_usd
        self.rules, self.costs = rules, costs
        self.positions: dict[str, Position] = {}
        self.fills: list[dict[str, Any]] = []
        self.closed_positions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.realized_pnl_usd = self.fees_usd = self.slippage_usd = self.funding_usd = 0.0
        self._next_id = 1

    def event(self, time_ms: int, kind: str, symbol: str = "", **details: Any) -> None:
        self.events.append({"time_ms": time_ms, "kind": kind, "symbol": symbol, **details})

    def views(self, marks: dict[str, float]) -> dict[str, PositionView]:
        return {
            s: PositionView(
                p.quantity,
                p.average_entry_price,
                p.stop_price,
                p.initial_risk_usd,
                p.quantity * marks[s],
            )
            for s, p in self.positions.items()
        }

    def equity(self, marks: dict[str, float]) -> float:
        return self.cash_usd + sum(p.quantity * marks[s] for s, p in self.positions.items())

    def snapshot(self, time_ms: int, marks: dict[str, float]) -> dict[str, Any]:
        unrealized = sum(
            p.quantity * (marks[s] - p.average_reference_price) for s, p in self.positions.items()
        )
        equity = self.equity(marks)
        identity = (
            self.initial_capital_usd
            + self.realized_pnl_usd
            + unrealized
            - self.fees_usd
            - self.slippage_usd
            + self.funding_usd
        )
        if abs(equity - identity) > 1e-6:
            raise ArithmeticError(f"equity ledger mismatch: {equity} != {identity}")
        notionals = [p.quantity * marks[s] for s, p in self.positions.items()]
        return {
            "time_ms": time_ms,
            "equity_usd": equity,
            "cash_usd": self.cash_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "unrealized_pnl_usd": unrealized,
            "fees_usd": self.fees_usd,
            "slippage_usd": self.slippage_usd,
            "funding_usd": self.funding_usd,
            "gross_exposure": sum(abs(n) for n in notionals) / equity if equity > 0 else 0.0,
            "net_exposure": sum(notionals) / equity if equity > 0 else 0.0,
            "position_count": len(notionals),
        }

    def _fill(
        self,
        symbol: str,
        quantity: float,
        base_price: float,
        time_ms: int,
        reason: str,
        target: TargetPosition,
        slippage_bps: float,
        fee_bps: float,
    ) -> None:
        if abs(quantity) < 1e-14:
            return
        price = base_price * (1 + math.copysign(slippage_bps / 10000, quantity))
        fee = abs(quantity) * price * fee_bps / 10000
        slip = quantity * (price - base_price)
        self.cash_usd -= quantity * price + fee
        self.fees_usd += fee
        self.slippage_usd += slip
        position = self.positions.get(symbol)
        realized = 0.0
        if position is None:
            position = Position(
                self._next_id,
                symbol,
                quantity,
                price,
                base_price,
                time_ms,
                target.stop_price,
                target.initial_risk_usd,
            )
            self._next_id += 1
            self.positions[symbol] = position
        elif position.quantity * quantity > 0:
            total = abs(position.quantity) + abs(quantity)
            position.average_entry_price = (
                abs(position.quantity) * position.average_entry_price + abs(quantity) * price
            ) / total
            position.average_reference_price = (
                abs(position.quantity) * position.average_reference_price
                + abs(quantity) * base_price
            ) / total
            position.quantity += quantity
        else:
            if abs(quantity) > abs(position.quantity) + 1e-10:
                raise ArithmeticError("a reversal must be split at zero")
            realized = (
                abs(quantity)
                * (base_price - position.average_reference_price)
                * math.copysign(1, position.quantity)
            )
            position.gross_pnl_usd += realized
            self.realized_pnl_usd += realized
            position.quantity += quantity
        position.fees_usd += fee
        position.slippage_usd += slip
        self.fills.append(
            {
                "time_ms": time_ms,
                "symbol": symbol,
                "side": "buy" if quantity > 0 else "sell",
                "quantity": abs(quantity),
                "signed_quantity": quantity,
                "price": price,
                "reference_price": base_price,
                "fee_usd": fee,
                "slippage_usd": slip,
                "reason": reason,
                "lifecycle_id": position.lifecycle_id,
                "realized_pnl_usd": realized,
            }
        )
        if abs(position.quantity) < 1e-10:
            self.closed_positions.append(self.position_record(position, time_ms, reason))
            del self.positions[symbol]

    def position_record(
        self, p: Position, exit_time_ms: int | None, reason: str, mark: float | None = None
    ) -> dict[str, Any]:
        unrealized = p.quantity * (mark - p.average_reference_price) if mark is not None else 0.0
        gross = p.gross_pnl_usd + unrealized
        net = gross - p.fees_usd - p.slippage_usd + p.funding_usd
        return {
            "lifecycle_id": p.lifecycle_id,
            "symbol": p.symbol,
            "entry_time_ms": p.entry_time_ms,
            "exit_time_ms": exit_time_ms,
            "exit_reason": reason,
            "remaining_quantity": p.quantity,
            "average_entry_price": p.average_entry_price,
            "gross_pnl_usd": gross,
            "net_pnl_usd": net,
            "fees_usd": p.fees_usd,
            "slippage_usd": p.slippage_usd,
            "funding_usd": p.funding_usd,
            "initial_risk_usd": p.initial_risk_usd,
            "return_r": net / p.initial_risk_usd if p.initial_risk_usd is not None else None,
        }

    def close(self, symbol: str, price: float, time_ms: int, reason: str) -> None:
        p = self.positions.get(symbol)
        if p is None:
            return
        slip = (
            self.costs.delist_slippage_bps
            if reason == "delisting"
            else self.costs.stop_slippage_bps
            if reason == "stop"
            else self.costs.slippage_bps
        )
        fee = (
            self.costs.maker_fee_bps
            if self.costs.rebalance_fill == "maker" and reason not in {"stop", "delisting"}
            else self.costs.taker_fee_bps
        )
        # Reduce-only exits must never be stranded by an entry minimum.
        self._fill(symbol, -p.quantity, price, time_ms, reason, TargetPosition(0), slip, fee)

    def rebalance(
        self,
        symbol: str,
        target: TargetPosition,
        reference_price: float,
        open_price: float,
        time_ms: int,
        reason: str = "signal",
    ) -> None:
        current = self.positions.get(symbol)
        if target.notional_usd == 0:
            self.close(symbol, open_price, time_ms, reason)
            return
        if current is not None and current.quantity * target.notional_usd < 0:
            self.close(symbol, open_price, time_ms, reason)
            current = None
        notional_delta = target.notional_usd - (
            current.quantity * reference_price if current else 0.0
        )
        if current:
            current.stop_price = target.stop_price
        if abs(notional_delta) < 1e-8:
            return
        sign = math.copysign(1, notional_delta)
        fill_price = open_price * (1 + sign * self.costs.slippage_bps / 10000)
        rules = self.rules[symbol]
        quantity = rules.quantity(notional_delta, fill_price)
        reducing = current is not None and current.quantity * sign < 0
        if reducing and current is not None:
            quantity = min(quantity, abs(current.quantity))
        if (
            quantity == 0
            or Decimal(str(quantity)) < rules.min_order_qty
            or Decimal(str(quantity)) * Decimal(str(fill_price)) < (rules.min_notional_value or 0)
        ):
            self.event(time_ms, "minimum_order_skip", symbol, notional_usd=notional_delta)
            return
        fee = (
            self.costs.taker_fee_bps
            if self.costs.rebalance_fill == "taker"
            else self.costs.maker_fee_bps
        )
        self._fill(
            symbol,
            sign * quantity,
            open_price,
            time_ms,
            reason,
            target,
            self.costs.slippage_bps,
            fee,
        )

    def funding(
        self, symbol: str, time_ms: int, rate: float | None, price: float, source: str
    ) -> None:
        p = self.positions[symbol]
        payment = -p.quantity * price * (rate or 0.0)
        p.funding_usd += payment
        self.funding_usd += payment
        self.cash_usd += payment
        self.event(
            time_ms,
            "funding",
            symbol,
            payment_usd=payment,
            price=price,
            price_source=source,
            rate_frac=rate,
            observed=rate is not None,
            quantity=p.quantity,
        )
