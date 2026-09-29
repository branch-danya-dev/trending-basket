"""Actual-position deltas, confirmed exchange stops and durable fill ownership."""

from __future__ import annotations

import hashlib
from contextlib import suppress
from dataclasses import asdict
from decimal import Decimal
from typing import Any, NoReturn

from trending_basket.execution.bybit_private import BybitPrivateClient, PrivateAPIError
from trending_basket.execution.journal import Halted, Journal, Notifier
from trending_basket.execution.planning import ExchangeRules, planned_delta, protected_stop
from trending_basket.portfolio.limits import PortfolioLimits, limit_violations
from trending_basket.strategies.base import PositionExit, TargetPosition

D = Decimal
TERMINAL = {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}


class LiveExecutor:
    def __init__(
        self,
        client: BybitPrivateClient,
        journal: Journal,
        notifier: Notifier,
        rules: dict[str, ExchangeRules],
    ) -> None:
        self.client, self.journal, self.notifier, self.rules = client, journal, notifier, rules
        self.recent_exits: list[PositionExit] = []
        self.closing_unprotected = False

    def actual(self) -> dict[str, dict[str, Any]]:
        result = {}
        for row in self.client.positions():
            if D(row["size"]) == 0:
                continue
            if row["side"] != "Buy" or int(row["positionIdx"]) != 0:
                self.stop("foreign short or hedge-mode position", cancel_entries=False)
            result[row["symbol"]] = row
        return result

    def stop(self, reason: str, *, cancel_entries: bool = True) -> NoReturn:
        self.journal.halt(reason)
        if cancel_entries:
            for link, order in self.journal.state["orders"].items():
                if not order["reduceOnly"] and order.get("status") not in TERMINAL:
                    try:
                        self.client.cancel_order(order["symbol"], link)
                    except (PrivateAPIError, Halted):
                        self.journal.append(
                            "events", kind="entry_cancel_failed", order_link_id=link
                        )
        self.notifier.send(f"DEMO STOP: {reason}. Exchange stops preserved.")
        raise Halted(reason)

    def _record_fills(self, fills: list[dict[str, Any]]) -> None:
        state = self.journal.state
        for fill in sorted(fills, key=lambda f: (int(f["execTime"]), f["execId"])):
            if (
                fill["execId"] in state["processed_fills"]
                or fill.get("execType", "Trade") != "Trade"
            ):
                continue
            symbol, link = fill["symbol"], fill.get("orderLinkId", "")
            order = state["orders"].get(link)
            stop_fill = fill["orderId"] in state["stop_ids"]
            position = state["positions"].get(symbol)
            intended = state.get("stop_intents", {}).get(symbol)
            if order is None and not stop_fill and position and intended:
                recovered = self.client.order_by_id(symbol, fill["orderId"])
                if recovered and self._valid_stop(
                    recovered, {"symbol": symbol, "size": position["quantity"]}, D(intended)
                ):
                    state["stop_ids"].append(fill["orderId"])
                    stop_fill = True
            if order is None and not stop_fill:
                self.stop("unowned execution found during reconciliation", cancel_entries=False)
            qty, price = D(fill["execQty"]), D(fill["execPrice"])
            signed = qty if fill["side"] == "Buy" else -qty
            held = D(position["quantity"]) if position else D(0)
            remaining = held + signed
            if remaining < 0:
                self.stop("unexpected short execution", cancel_entries=False)
            reference = float(order["reference_price"]) if order else float(position["stop_price"])
            decision_price = (
                float(order["reference_price"])
                if order
                else float(position.get("decision_price", reference))
            )
            slip = (float(price) / decision_price - 1) * 10000 * (1 if signed > 0 else -1)
            reference_slip = (float(price) / reference - 1) * 10000 * (1 if signed > 0 else -1)
            if remaining:
                if position is None:
                    position = dict(
                        quantity="0",
                        average_entry_price=float(price),
                        entry_time_ms=int(fill["execTime"]),
                        lifecycle_id=int(fill["execTime"]),
                        stop_price=float(order["stop_price"]),
                        initial_risk_usd=order.get("initial_risk_usd"),
                        unprotected_since_ms=int(fill["execTime"]),
                    )
                    state["positions"][symbol] = position
                if signed > 0:
                    position["average_entry_price"] = float(
                        (held * D(str(position["average_entry_price"])) + qty * price) / remaining
                    )
                    position["unprotected_since_ms"] = min(
                        position.get("unprotected_since_ms") or int(fill["execTime"]),
                        int(fill["execTime"]),
                    )
                position["quantity"] = str(remaining)
                if order:
                    position["decision_price"] = decision_price
            else:
                state["positions"].pop(symbol, None)
                if stop_fill:
                    event = PositionExit(symbol, int(fill["execTime"]), "stop")
                    self.recent_exits.append(event)
                    state.setdefault("recent_exits", []).append(asdict(event))
            state["processed_fills"].append(fill["execId"])
            self.journal.append(
                "fills",
                exec_id=fill["execId"],
                order_id=fill["orderId"],
                order_link_id=link,
                symbol=symbol,
                side=fill["side"],
                quantity=str(qty),
                execution_price=str(price),
                decision_price=decision_price,
                reference_price=reference,
                slippage_bps=slip,
                reference_slippage_bps=reference_slip,
                fee_usd=float(fill["execFee"]),
                fee_currency=fill.get("feeCurrency", "USDT"),
                exchange_time_ms=int(fill["execTime"]),
                reason="stop" if stop_fill else order.get("reason", "signal"),
            )
            self.journal.save()
            self.notifier.send(
                f"DEMO fill {symbol} {fill['side']} qty={qty} price={price}; "
                f"fee={fill['execFee']}; slippage={slip:.3f} bps"
            )

    def reconcile(self) -> dict[str, dict[str, Any]]:
        state = self.journal.state
        last_poll = state["last_poll_ms"]
        now = self.client.now_ms()
        if last_poll is not None:
            if now - last_poll > 7 * 86400000:
                self.stop(
                    "offline longer than Demo history retention; reconciliation incomplete",
                    cancel_entries=False,
                )
            self._record_fills(
                self.client.executions(
                    startTime=max(state.get("started_ms", last_poll), now - 7 * 86400000 + 1),
                    endTime=now,
                )
            )
        actual = self.actual()
        if self.client.foreign_markets():
            self.stop("foreign position or order on another market", cancel_entries=False)
        expected = {s: D(p["quantity"]) for s, p in state["positions"].items()}
        if {s: D(p["size"]) for s, p in actual.items()} != expected:
            self.stop("position mismatch or foreign position", cancel_entries=False)
        for order in self.client.open_orders():
            symbol = order["symbol"]
            intended = state.get("stop_intents", {}).get(symbol)
            if (
                symbol in actual
                and intended
                and self._valid_stop(order, actual[symbol], D(intended))
            ):
                state["stop_ids"] = sorted(set(state["stop_ids"]) | {order["orderId"]})
            if (
                order.get("orderLinkId") not in state["orders"]
                and order["orderId"] not in state["stop_ids"]
            ):
                self.stop("foreign open order", cancel_entries=False)
        state.setdefault("started_ms", now)
        state["last_poll_ms"] = now
        self.journal.save()
        self.journal.append(
            "positions",
            positions={
                s: dict(
                    quantity=p["size"],
                    entry=p["avgPrice"],
                    stop=p["stopLoss"],
                    liquidation=p["liqPrice"],
                )
                for s, p in actual.items()
            },
        )
        return actual

    def _submit(
        self,
        symbol: str,
        delta: Decimal,
        reference: float,
        at_ms: int,
        target: TargetPosition,
        reason: str,
    ) -> None:
        if not (self.closing_unprotected and delta < 0):
            self.journal.require_running()
        state = self.journal.state
        if state["pending_order"]:
            raise Halted("pending order requires recovery before another submit")
        seq = sum(
            o["decision_ms"] == at_ms and o["symbol"] == symbol for o in state["orders"].values()
        )
        digest = hashlib.sha256(symbol.encode()).hexdigest()[:10]
        link = f"tb-{at_ms // 86400000}-{digest}-{seq:04d}"
        params = dict(
            category="linear",
            symbol=symbol,
            side="Buy" if delta > 0 else "Sell",
            orderType="Market",
            qty=str(abs(delta)),
            orderLinkId=link,
            positionIdx=0,
            reduceOnly=delta < 0,
        )
        if delta > 0:
            if target.stop_price is None:
                raise ValueError("entry requires exchange stop")
            stop = protected_stop(
                target.stop_price,
                D(str(state["positions"].get(symbol, {}).get("stop_price", 0))),
                self.rules[symbol].tick_size,
            )
            params.update(stopLoss=str(stop), slTriggerBy="LastPrice", tpslMode="Full")
            state.setdefault("stop_intents", {})[symbol] = str(stop)
        order = dict(
            **params,
            decision_ms=at_ms,
            reference_price=reference,
            stop_price=target.stop_price,
            initial_risk_usd=target.initial_risk_usd,
            status="Intent",
            reason=reason,
        )
        state["orders"][link] = order
        state["pending_order"] = dict(params=params, created_ms=self.client.now_ms())
        self.journal.save()  # Write-ahead intent precedes the network side effect.
        self.journal.append("orders", kind="intent", **order)
        self.recover_order()

    def recover_order(self) -> None:
        state = self.journal.state
        pending = state["pending_order"]
        if not pending:
            return
        params = pending["params"]
        link, symbol = params["orderLinkId"], params["symbol"]
        if self.client.now_ms() - pending["created_ms"] > 7 * 86400000:
            self.stop(
                "order intent older than Demo retention; refusing resend", cancel_entries=False
            )
        self._record_fills(self.client.executions(orderLinkId=link))
        self.reconcile()
        existing = self.client.find_order(link, symbol)
        latest_day = (self.client.now_ms() - 180000) // 86400000 * 86400000
        if existing is None and state["orders"][link]["decision_ms"] < latest_day:
            self.stop(
                "unobserved previous-day intent; refusing to submit stale decision",
                cancel_entries=False,
            )
        self.client.create_order(
            params, allow_submit=None if self.closing_unprotected else self.journal.require_running
        )
        deadline = self.client.now_ms() + 30_000
        while True:
            order = self.client.find_order(link, symbol)
            fills = self.client.executions(orderLinkId=link)
            self._record_fills(fills)
            terminal = order is not None and order["orderStatus"] in TERMINAL
            recorded = sum(D(f["execQty"]) for f in fills if f.get("execType", "Trade") == "Trade")
            confirmed = (
                order is not None and terminal and recorded == D(order.get("cumExecQty", "0"))
            )
            if confirmed:
                assert order is not None
                state["orders"][link]["status"] = order["orderStatus"]
                state["pending_order"] = None
                self.journal.save()
                self.journal.append(
                    "orders", kind="terminal", order_link_id=link, status=order["orderStatus"]
                )
            # Confirm protection after every fill, including partial fills and reductions.
            actual = self.actual().get(symbol)
            if actual and not self.closing_unprotected:
                stop = (
                    state["orders"][link]["stop_price"] or state["positions"][symbol]["stop_price"]
                )
                self.confirm_stop(symbol, float(stop), actual)
            if confirmed:
                self.journal.operation_succeeded()
                return
            if self.client.now_ms() >= deadline:
                self.stop("order/fill confirmation timeout; reconcile before resume")
            self.client.sleep(1)

    @staticmethod
    def _valid_stop(order: dict[str, Any], position: dict[str, Any], price: Decimal) -> bool:
        return bool(
            order["symbol"] == position["symbol"]
            and order.get("stopOrderType") == "StopLoss"
            and order.get("triggerBy") == "LastPrice"
            and D(order.get("triggerPrice") or "0") >= price
            and order.get("reduceOnly")
            and order.get("side") == "Sell"
            and D(order.get("qty") or "0") >= D(position["size"])
        )

    def _cancel_pending(self) -> None:
        pending = self.journal.state["pending_order"]
        if not pending:
            return
        params = pending["params"]
        link, symbol = params["orderLinkId"], params["symbol"]
        # A terminal IOC may already be absent from the open-order cache.
        with suppress(PrivateAPIError):
            self.client.cancel_order(symbol, link)
        deadline = self.client.now_ms() + 30_000
        while self.client.now_ms() < deadline:
            order = self.client.find_order(link, symbol)
            fills = self.client.executions(orderLinkId=link)
            self._record_fills(fills)
            if (
                order
                and order["orderStatus"] in TERMINAL
                and sum(D(f["execQty"]) for f in fills) == D(order.get("cumExecQty", "0"))
            ):
                self.journal.state["orders"][link]["status"] = order["orderStatus"]
                self.journal.state["pending_order"] = None
                self.journal.save()
                return
            self.client.sleep(1)
        self.stop(
            "entry cancellation unconfirmed; "
            "emergency close cannot safely supersede pending intent",
            cancel_entries=False,
        )

    def close_unprotected(self, symbol: str, reason: str) -> None:
        self.journal.append("events", kind="emergency_close", symbol=symbol, reason=reason)
        self._cancel_pending()
        self.closing_unprotected = True
        try:
            for _ in range(12):
                current = self.actual().get(symbol)
                if current is None:
                    break
                amount = min(D(current["size"]), self.rules[symbol].max_market_qty)
                self._submit(
                    symbol,
                    -amount,
                    float(current["markPrice"]),
                    self.client.now_ms() // 86400000 * 86400000,
                    TargetPosition(0),
                    "unprotected_exit",
                )
            else:
                self.stop(
                    "emergency close incomplete; manual inspection required", cancel_entries=False
                )
        finally:
            self.closing_unprotected = False
        self.stop(reason)

    def confirm_stop(
        self, symbol: str, proposed: float, actual: dict[str, Any] | None = None
    ) -> None:
        actual = actual or self.actual().get(symbol)
        if actual is None:
            return
        position = self.journal.state["positions"][symbol]
        stop = protected_stop(
            proposed,
            max(D(actual.get("stopLoss") or "0"), D(str(position.get("stop_price") or 0))),
            self.rules[symbol].tick_size,
        )
        if stop <= 0:
            self.close_unprotected(symbol, "invalid protective stop")
        started = position.get("unprotected_since_ms") or self.client.now_ms()
        position["unprotected_since_ms"] = started
        self.journal.state.setdefault("stop_intents", {})[symbol] = str(stop)
        self.journal.save()
        while self.client.now_ms() - started < 60_000:
            try:
                # Reducing/protecting an existing fill remains necessary after an API error halt.
                # No new entry is allowed by _submit while that halt flag is set.
                if D(self.client.ticker(symbol)["lastPrice"]) <= stop:
                    self.close_unprotected(
                        symbol, "last price crossed protective stop before confirmation"
                    )
                self.client.set_stop(symbol, str(stop))
                current = self.actual().get(symbol)
                if current is None:
                    return
                if D(current.get("stopLoss") or "0") >= stop:
                    protected = [
                        o for o in self.client.open_orders() if self._valid_stop(o, current, stop)
                    ]
                    if protected:
                        self.journal.state["stop_ids"] = sorted(
                            set(self.journal.state["stop_ids"]) | {o["orderId"] for o in protected}
                        )
                        position["stop_price"], position["unprotected_since_ms"] = float(stop), None
                        self.journal.save()
                        self.journal.append(
                            "events",
                            kind="stop_confirmed",
                            symbol=symbol,
                            stop_price=str(stop),
                            quantity=current["size"],
                        )
                        self.journal.operation_succeeded()
                        return
            except PrivateAPIError as exc:
                if exc.code == 10002:
                    self.stop(
                        "exchange time is not synchronized; exchange stops preserved",
                        cancel_entries=False,
                    )
            self.client.sleep(1)
        self.close_unprotected(symbol, "position stop unconfirmed after 60 seconds")

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
        self.journal.require_running()
        actual = self.actual().get(symbol)
        held = D(actual["size"]) if actual else D(0)
        delta, adjustment = planned_delta(
            target.notional_usd,
            held,
            D(str(open_price)),
            self.rules[symbol],
            allow_increase=allow_increase,
        )
        self.journal.append(
            "events",
            kind=adjustment,
            symbol=symbol,
            held_quantity=str(held),
            requested_delta=str(delta),
            target_notional_usd=target.notional_usd,
        )
        if delta > 0:
            if target.stop_price is None:
                raise ValueError("entry requires a stop")
            if D(str(open_price)) <= protected_stop(
                target.stop_price,
                D(actual.get("stopLoss") or "0") if actual else D(0),
                self.rules[symbol].tick_size,
            ):
                self.journal.append(
                    "events",
                    kind="entry_skipped",
                    symbol=symbol,
                    reason="price already beyond decision stop",
                )
                if actual:
                    self.close_unprotected(symbol, "market crossed decision stop")
                return
            self.client.set_leverage(symbol)
        # A restart resumes the remaining requested chunks, never the unfilled part of an IOC.
        pending = self.journal.state["pending"]
        if pending and pending["day_ms"] == time_ms:
            plans = pending.setdefault("execution_plan", {})
            if symbol not in plans:
                plans[symbol] = str(delta)
                self.journal.save()
            delta = D(plans[symbol])
            requested = sum(
                D(o["qty"])
                for o in self.journal.state["orders"].values()
                if o["symbol"] == symbol and o["decision_ms"] == time_ms and o["reason"] == reason
            )
        else:
            requested = D(0)
        # Split only at the exchange market-order ceiling, with deterministic sequential IDs.
        remaining = max(D(0), abs(delta) - requested)
        while remaining:
            amount = min(remaining, self.rules[symbol].max_market_qty)
            if amount <= 0:
                raise ValueError("invalid market quantity limit")
            self._submit(
                symbol, amount if delta > 0 else -amount, reference_price, time_ms, target, reason
            )
            remaining -= amount
        actual = self.actual().get(symbol)
        if actual:
            self.confirm_stop(symbol, target.stop_price or float(actual["stopLoss"]), actual)
            self.ensure_liquidation_distance(symbol, reference_price, time_ms)

    def ensure_liquidation_distance(self, symbol: str, reference: float, at_ms: int) -> None:
        for _ in range(12):
            position = self.actual().get(symbol)
            if not position:
                return
            entry, stop = D(position["avgPrice"]), D(position["stopLoss"])
            liquidation = position.get("liqPrice")
            if liquidation and entry - D(liquidation) >= D("1.5") * abs(entry - stop):
                return
            quantity = D(position["size"])
            # No invented liquidation price when Bybit returns an empty field.
            reduce = (
                quantity
                if not liquidation
                else max(self.rules[symbol].quantity.minimum_quantity(reference), quantity / 2)
            )
            step = self.rules[symbol].quantity.qty_step
            reduce = min(
                quantity, (reduce / step).to_integral_value(rounding="ROUND_CEILING") * step
            )
            self._submit(
                symbol, -reduce, reference, at_ms, TargetPosition(0), "liquidation_distance"
            )
            self.notifier.send(f"DEMO {symbol}: reduced {reduce} for liquidation distance")
            self.journal.append(
                "events",
                kind="liquidation_reduction",
                symbol=symbol,
                quantity=str(reduce),
                liquidation=liquidation or "unknown",
            )
        self.stop("cannot establish liquidation distance")

    def enforce_limits(
        self, marks: dict[str, float], time_ms: int, limits: PortfolioLimits
    ) -> None:
        # Use current exchange equity/marks AFTER fills and fees; repair only toward less risk.
        for _ in range(100):
            actual = self.actual()
            equity = float(self.client.wallet()["totalEquity"])
            notionals = {s: float(p["size"]) * float(p["markPrice"]) for s, p in actual.items()}
            exceeded = limit_violations(notionals, equity, limits)
            if not exceeded:
                self.journal.append(
                    "events", kind="post_rebalance", equity_usd=equity, notionals=notionals
                )
                return
            symbol = max(notionals, key=lambda s: notionals[s])
            quantity = D(actual[symbol]["size"])
            reduction = min(
                quantity,
                max(
                    self.rules[symbol].quantity.minimum_quantity(
                        float(actual[symbol]["markPrice"])
                    ),
                    quantity / 2,
                ),
            )
            step = self.rules[symbol].quantity.qty_step
            reduction = min(
                quantity, (reduction / step).to_integral_value(rounding="ROUND_CEILING") * step
            )
            self._submit(
                symbol,
                -reduction,
                marks[symbol],
                time_ms,
                TargetPosition(0),
                "actual_limit_reduction",
            )
        self.stop("actual exposure repair did not converge")
