"""Deterministic fills, instrument rounding and an independent cash/PnL ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any, Literal

from trending_basket.backtest.config import Costs
from trending_basket.portfolio.limits import (
    PortfolioLimits,
    actual_exposures,
    assert_actual_limits,
    limit_violations,
)
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

    def minimum_quantity(self, price: float) -> Decimal:
        raw = max(self.min_order_qty, (self.min_notional_value or Decimal(0)) / Decimal(str(price)))
        return (raw / self.qty_step).to_integral_value(rounding=ROUND_CEILING) * self.qty_step


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
    direction: str = "long"
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    funding_usd: float = 0.0


class SimExecutor:
    def __init__(
        self,
        capital_usd: float,
        rules: dict[str, InstrumentRules],
        costs: Costs,
        quantity_mode: Literal["exchange", "exact"] = "exchange",
    ) -> None:
        self.quantity_mode = quantity_mode
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
                p.entry_time_ms,
                p.lifecycle_id,
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
        if quantity == 0 if self.quantity_mode == "exact" else abs(quantity) < 1e-14:
            return
        price = base_price * (1 + math.copysign(slippage_bps / 10000, quantity))
        fee = abs(quantity) * price * fee_bps / 10000
        slip = quantity * (price - base_price)
        self.cash_usd -= quantity * price + fee
        self.fees_usd += fee
        self.slippage_usd += slip
        position = self.positions.get(symbol)
        realized = 0.0
        executed_risk = None
        if target.initial_risk_usd is not None and target.notional_usd:
            total_quantity = abs(quantity) + (abs(position.quantity) if position else 0.0)
            executed_risk = (
                target.initial_risk_usd * total_quantity * base_price / abs(target.notional_usd)
            )
        if position is None:
            position = Position(
                self._next_id,
                symbol,
                quantity,
                price,
                base_price,
                time_ms,
                target.stop_price,
                executed_risk,
                "long" if quantity > 0 else "short",
            )
            self._next_id += 1
            self.positions[symbol] = position
        elif position.quantity * quantity > 0:
            if executed_risk is not None:
                position.initial_risk_usd = max(position.initial_risk_usd or 0.0, executed_risk)
            total = abs(position.quantity) + abs(quantity)
            position.average_entry_price = (
                abs(position.quantity) * position.average_entry_price + abs(quantity) * price
            ) / total
            position.average_reference_price = (
                abs(position.quantity) * position.average_reference_price
                + abs(quantity) * base_price
            ) / total
            position.quantity = float(Decimal(str(position.quantity)) + Decimal(str(quantity)))
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
            position.quantity = float(Decimal(str(position.quantity)) + Decimal(str(quantity)))
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
        if (
            position.quantity == 0
            if self.quantity_mode == "exact"
            else abs(position.quantity) < 1e-10
        ):
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
            "direction": p.direction,
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
        *,
        allow_increase: bool = True,
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
        if notional_delta == 0 if self.quantity_mode == "exact" else abs(notional_delta) < 1e-8:
            return
        direction = target.notional_usd - (current.quantity * open_price if current else 0.0)
        sign = math.copysign(1, direction)
        fill_price = open_price * (1 + sign * self.costs.slippage_bps / 10000)
        rules = self.rules[symbol]
        # Quantize the TARGET position, not its delta: a rounded reduction must not
        # leave an executed position larger than the target. Exact holds above do not drift.
        target_quantity = (
            abs(target.notional_usd) / fill_price
            if self.quantity_mode == "exact"
            else rules.quantity(target.notional_usd, fill_price)
        )
        held = abs(current.quantity) if current else 0.0
        if not allow_increase:
            target_quantity = min(target_quantity, held)
        if current and target_quantity < held:
            self.reduce_to(symbol, target_quantity, open_price, time_ms, reason)
            return
        quantity = float(Decimal(str(target_quantity)) - Decimal(str(held)))
        sign = math.copysign(1, target.notional_usd)
        if self.quantity_mode == "exact" and quantity == 0:
            return
        if self.quantity_mode == "exchange" and (
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

    def reduce_to(
        self, symbol: str, target_quantity: float, open_price: float, time_ms: int, reason: str
    ) -> None:
        """Meet an absolute quantity ceiling, expanding a too-small reduce-only order."""
        current = self.positions[symbol]
        rules = self.rules[symbol]
        held = Decimal(str(abs(current.quantity)))
        target = Decimal(str(target_quantity))
        if target >= held:
            return
        sign = -math.copysign(1, current.quantity)
        price = open_price * (1 + sign * self.costs.slippage_bps / 10000)
        requested = held - target
        if self.quantity_mode == "exact":
            quantity = min(held, requested)
        else:
            lots = (requested / rules.qty_step).to_integral_value(rounding=ROUND_CEILING)
            quantity = min(held, max(lots * rules.qty_step, rules.minimum_quantity(price)))
        if quantity > requested:
            self.event(
                time_ms,
                "reduction_minimum_adjustment",
                symbol,
                requested_quantity=float(requested),
                executed_quantity=float(quantity),
                target_quantity=target_quantity,
                remaining_quantity=float(held - quantity),
            )
        if quantity == held:
            self.close(symbol, open_price, time_ms, reason)
        else:
            fee = (
                self.costs.taker_fee_bps
                if self.costs.rebalance_fill == "taker"
                else self.costs.maker_fee_bps
            )
            self._fill(
                symbol,
                sign * float(quantity),
                open_price,
                time_ms,
                reason,
                TargetPosition(0),
                self.costs.slippage_bps,
                fee,
            )

    def enforce_limits(
        self, marks: dict[str, float], time_ms: int, limits: PortfolioLimits
    ) -> None:
        """Reduce executed risk until all limits hold AFTER fees, slippage and rounding."""
        repairs, accelerated_after = 0, 4 * len(self.positions) + 16
        while True:
            notionals = {s: p.quantity * marks[s] for s, p in self.positions.items()}
            equity = self.equity(marks)
            exceeded = limit_violations(notionals, equity, limits)
            if not exceeded:
                break
            # A net violation is repaired only on the side contributing to its sign.
            net = sum(notionals.values())
            kind = "net_exposure" if "net_exposure" in exceeded else exceeded[0]
            eligible = [s for s, n in notionals.items() if kind != "net_exposure" or n * net > 0]
            symbol = min(eligible, key=lambda s: (-abs(notionals[s]), s))
            position = self.positions[symbol]
            before_qty = abs(position.quantity)
            target_qty = 0.0
            if kind != "capital_depleted" and repairs < accelerated_after:
                cap = float(getattr(limits, f"max_{kind}"))
                measure = (
                    abs(net)
                    if kind == "net_exposure"
                    else sum(abs(n) for n in notionals.values())
                    if kind == "gross_exposure"
                    else abs(notionals[symbol])
                )
                fee = (
                    self.costs.taker_fee_bps
                    if self.costs.rebalance_fill == "taker"
                    else self.costs.maker_fee_bps
                )
                slip = self.costs.slippage_bps / 10000
                # Closing notional x costs c*x, so x >= (measure-cap*E)/(1-cap*c).
                cost_frac = slip + fee / 10000 * (1 - math.copysign(slip, position.quantity))
                denominator = 1 - cap * cost_frac
                if denominator > 0:
                    reduction = (measure - cap * equity) / denominator / marks[symbol]
                    target_qty = max(0.0, before_qty - reduction)
            before_equity = equity
            self.reduce_to(symbol, target_qty, marks[symbol], time_ms, "limit")
            after_qty = abs(self.positions[symbol].quantity) if symbol in self.positions else 0.0
            if after_qty >= before_qty:
                raise ArithmeticError("exposure repair made no progress")
            self.event(
                time_ms,
                "actual_limit_reduction",
                symbol,
                limit=kind,
                before_quantity=before_qty,
                after_quantity=after_qty,
                equity_before_usd=before_equity,
                equity_after_usd=self.equity(marks),
                full_close_fallback=repairs >= accelerated_after,
            )
            repairs += 1
        notionals = {s: p.quantity * marks[s] for s, p in self.positions.items()}
        equity = self.equity(marks)
        assert_actual_limits(notionals, equity, limits)
        self.event(
            time_ms, "post_rebalance", "", equity_usd=equity, **actual_exposures(notionals, equity)
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
