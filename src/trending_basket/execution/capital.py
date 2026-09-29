"""Owned execution/funding ledger, independent of account collateral valuation."""

from typing import Any

from trending_basket.execution.bybit_private import BybitPrivateClient
from trending_basket.execution.journal import Journal
from trending_basket.execution.margin import D, number


def initialize(journal: Journal, capital: float | None, now_ms: int) -> None:
    if capital is None or number(capital) <= 0:
        raise ValueError("TB_ALLOCATED_CAPITAL_USD must be positive")
    state = journal.state
    if state.get("capital") is not None:
        if number(state["capital"]["allocated_usd"]) != number(capital):
            raise ValueError("allocated capital changed during active segment")
        return
    legacy = bool(state["orders"])
    if legacy and (state["positions"] or state["pending_order"] or state["pending"]):
        raise ValueError("legacy capital migration requires flat reconciled account")
    previous_day, previous_peak = state["last_decision_ms"], state["peak_equity_usd"]
    if legacy:
        state["legacy_wallet_checkpoint"] = dict(
            last_decision_ms=previous_day,
            peak_equity_usd=previous_peak,
        )
        state["last_decision_ms"] = None
        # Explicit one-time replay authorized for the first allocated-capital cycle.
        # Keep subsystem signals; allow the same closed bar to supply its current target.
        for value in state["strategy_states"].values():
            value["last_close_ms"] = -1
    state["capital"] = dict(allocated_usd=str(capital), start_ms=now_ms, fills={}, funding={})
    state["peak_equity_usd"] = capital
    journal.save()
    journal.append(
        "events",
        kind="allocated_capital_initialized",
        allocated_usd=capital,
        legacy_segment=legacy,
        previous_day_ms=previous_day,
        previous_peak=previous_peak,
    )


def record_fill(journal: Journal, fill: dict[str, Any], realized: D) -> None:
    capital = journal.state.get("capital")
    if capital is None or int(fill["execTime"]) < capital["start_ms"]:
        return
    if fill.get("feeCurrency", "USDT") not in ("USDT", ""):
        raise ValueError("non-USDT execution fee cannot enter strategy ledger")
    capital["fills"][fill["execId"]] = dict(
        symbol=fill["symbol"],
        time_ms=int(fill["execTime"]),
        side=fill["side"],
        quantity=fill["execQty"],
        realized_usd=str(realized),
        fee_usd=fill["execFee"],
    )


def sync_funding(journal: Journal, client: BybitPrivateClient) -> None:
    capital = journal.state.get("capital")
    if capital is None:
        return
    now = client.now_ms()
    last = capital.get("funding_checked_ms", capital["start_ms"])
    if now - last > 7 * 86400000:
        raise ValueError("funding reconciliation gap exceeds seven days")
    start = max(capital["start_ms"], now - 7 * 86400000 + 1)
    transactions = client.transactions(start, now)
    for row in transactions:
        stamp = int(row["transactionTime"])
        if not start <= stamp <= now or row.get("type") != "SETTLEMENT":
            continue
        amount = number(row.get("funding") or "0")
        if amount == 0:
            continue
        if row.get("category") != "linear" or row.get("currency") != "USDT":
            raise ValueError("unexpected funding currency or category")
        key, symbol = row.get("id"), row["symbol"]
        if not key:
            raise ValueError("funding transaction has no id")
        if key in capital["funding"]:
            continue
        owned = sum(
            number(f["quantity"]) * (1 if f["side"] == "Buy" else -1)
            for f in capital["fills"].values()
            if f["symbol"] == symbol and f["time_ms"] <= stamp
        )
        if owned <= 0 or (row.get("size") and abs(number(row["size"])) != owned):
            raise ValueError("funding does not match historical bot position")
        item = dict(symbol=symbol, time_ms=stamp, funding_usd=str(amount))
        capital["funding"][key] = item
        journal.append(
            "funding",
            transaction_id=key,
            symbol=symbol,
            funding_time_ms=stamp,
            funding_usd=str(amount),
        )
    capital["funding_checked_ms"] = now
    journal.save()


def snapshot(journal: Journal, actual: dict[str, dict[str, Any]]) -> dict[str, float]:
    capital = journal.state["capital"]
    realized = sum((number(f["realized_usd"]) for f in capital["fills"].values()), D(0))
    fees = sum((number(f["fee_usd"]) for f in capital["fills"].values()), D(0))
    funding = sum((number(f["funding_usd"]) for f in capital["funding"].values()), D(0))
    unrealized = sum((number(p.get("unrealisedPnl")) for p in actual.values()), D(0))
    allocated = number(capital["allocated_usd"])
    return dict(
        allocated_capital_usd=float(allocated),
        realized_pnl_usd=float(realized),
        fees_usd=float(fees),
        funding_usd=float(funding),
        unrealized_pnl_usd=float(unrealized),
        equity_usd=float(allocated + realized - fees + funding + unrealized),
    )
