"""Independent strategy replay and historical allocated-capital valuation."""

from __future__ import annotations

import copy
from decimal import Decimal as D
from typing import Any

from trending_basket.backtest.config import Experiment
from trending_basket.backtest.inputs import Inputs
from trending_basket.domain.types import Interval
from trending_basket.execution.journal import Halted, Journal

DAY = 86400000


def records(journal: Journal, channel: str) -> list[dict[str, Any]]:
    legacy = [
        r["record"] for r in journal.read_rows("legacy") if r.get("original_channel") == channel
    ]
    return legacy + journal.read_rows(channel)


def fills(journal: Journal) -> list[dict[str, Any]]:
    start = journal.state["capital"]["start_ms"]
    unique = {r["exec_id"]: r for r in records(journal, "fills") if r["exchange_time_ms"] >= start}
    return sorted(unique.values(), key=lambda r: (r["exchange_time_ms"], r["exec_id"]))


def historical_book(journal: Journal, at_ms: int) -> tuple[dict[str, Any], D, D]:
    book: dict[str, Any] = {}
    realized, fees = D(0), D(0)
    for row in fills(journal):
        if row["exchange_time_ms"] > at_ms:
            continue
        symbol, quantity, price = row["symbol"], D(row["quantity"]), D(row["execution_price"])
        old = book.get(symbol)
        fees += D(str(row["fee_usd"]))
        if row["side"] == "Buy":
            held = D(old["quantity"]) if old else D(0)
            average = (
                (held * D(str(old["average_entry_price"])) + quantity * price) / (held + quantity)
                if old
                else price
            )
            order = journal.state["orders"].get(row["order_link_id"], {})
            book[symbol] = dict(
                quantity=str(held + quantity),
                average_entry_price=float(average),
                entry_time_ms=old["entry_time_ms"] if old else row["exchange_time_ms"],
                lifecycle_id=old["lifecycle_id"] if old else row["exchange_time_ms"],
                stop_price=old["stop_price"] if old else order.get("stop_price"),
                initial_risk_usd=order.get("initial_risk_usd"),
            )
        else:
            if old is None or quantity > D(old["quantity"]):
                raise Halted("journal fill history cannot reconstruct position")
            realized += (price - D(str(old["average_entry_price"]))) * quantity
            remaining = D(old["quantity"]) - quantity
            if remaining:
                old["quantity"] = str(remaining)
            else:
                book.pop(symbol)
    for event in records(journal, "events"):
        if (
            event.get("kind") == "stop_confirmed"
            and event["time_ms"] <= at_ms
            and event["symbol"] in book
        ):
            p = book[event["symbol"]]
            if event["time_ms"] >= p["entry_time_ms"]:
                p["stop_price"] = max(p.get("stop_price") or 0, float(event["stop_price"]))
    return book, realized, fees


def verify_capital(journal: Journal, actual: dict[str, Any]) -> dict[str, float]:
    from trending_basket.execution.capital import snapshot

    book, realized, fees = historical_book(journal, journal.clock.now_ms() + 5000)
    if {s: D(p["quantity"]) for s, p in book.items()} != {
        s: D(p["size"]) for s, p in actual.items()
    }:
        raise Halted("journal quantities differ from exchange")
    funding_rows = {r["transaction_id"]: r for r in records(journal, "funding")}
    funding = sum((D(str(r["funding_usd"])) for r in funding_rows.values()), D(0))
    values = snapshot(journal, actual)
    for key, expected in (
        ("realized_pnl_usd", realized),
        ("fees_usd", fees),
        ("funding_usd", funding),
    ):
        if abs(values[key] - float(expected)) > 1e-9:
            raise Halted(f"journal capital mismatch: {key}")
    for symbol, position in book.items():
        if abs(float(position["average_entry_price"]) - float(actual[symbol]["avgPrice"])) > 1e-7:
            raise Halted(f"exchange entry price differs from reconstructed fills: {symbol}")
    return values


def reconstruct_equity(journal: Journal, inputs: Inputs, now_ms: int) -> None:
    start = journal.state["capital"]["start_ms"]
    existing = {r["at_ms"]: r for r in journal.read_rows("equity") if r.get("reconstructed")}
    funding_rows = {r["transaction_id"]: r for r in records(journal, "funding")}
    for at in range((start // DAY + 1) * DAY, now_ms // DAY * DAY + 1, DAY):
        book, realized, fees = historical_book(journal, at)
        unrealized = D(0)
        for symbol, position in book.items():
            bar = inputs.data.bar(symbol, Interval.D1, at - DAY)
            if bar is None:
                raise Halted(f"missing daily close for reconstructed equity: {symbol} {at}")
            unrealized += D(position["quantity"]) * (
                D(str(bar.close)) - D(str(position["average_entry_price"]))
            )
        funding = sum(
            (D(str(r["funding_usd"])) for r in funding_rows.values() if r["funding_time_ms"] <= at),
            D(0),
        )
        value = (
            D(journal.state["capital"]["allocated_usd"]) + realized - fees + funding + unrealized
        )
        previous = existing.get(at)
        if previous and abs(previous["equity_usd"] - float(value)) < 1e-9:
            continue
        journal.append(
            "equity",
            at_ms=at,
            equity_usd=float(value),
            reconstructed=True,
            supersedes_seq=previous.get("seq") if previous else None,
        )
        journal.state["peak_equity_usd"] = max(journal.state["peak_equity_usd"], float(value))


def replay_strategy(journal: Journal, config: Experiment, inputs: Inputs) -> dict[str, Any]:
    from trending_basket.execution.cycle import decide

    origin = journal.state.get("strategy_origin", {})
    states: dict[str, Any] = copy.deepcopy(origin)
    for row in journal.read_rows("strategy"):
        if row["seq"] not in journal.state.get("strategy_steps", []):
            continue
        context = copy.deepcopy(row["context"])
        context["strategy_states"] = states
        _, states, _ = decide(config, inputs, row["day_ms"], row["equity_usd"], context)
        if states != row["result"]:
            raise Halted(f"independent strategy replay differs at {row['day_ms']}")
    if states != journal.state["strategy_states"]:
        raise Halted("strategy snapshot differs from independent replay")
    return states


def strategy_step(
    journal: Journal,
    config: Experiment,
    inputs: Inputs,
    day: int,
    equity: float,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any], dict[str, float]]:
    from trending_basket.execution.cycle import decide

    source = copy.deepcopy(journal.state if context is None else context)
    consumed = journal.state.get("strategy_exit_keys", [])

    def exit_key(event: dict[str, Any]) -> str:
        return f"{event['symbol']}:{event['time_ms']}:{event['reason']}"

    source["recent_exits"] = [
        e for e in source.get("recent_exits", []) if exit_key(e) not in consumed
    ]
    target, states, marks = decide(config, inputs, day, equity, source)
    if journal.ledger is not None:
        clean = {
            k: copy.deepcopy(source.get(k, {} if k != "recent_exits" else []))
            for k in ("positions", "protected_reentries", "recent_exits")
        }
        journal.append("strategy", day_ms=day, equity_usd=equity, context=clean, result=states)
        journal.state.setdefault("strategy_steps", []).append(journal.ledger.records[-1]["seq"])
        journal.state.setdefault("strategy_exit_keys", []).extend(
            exit_key(e) for e in source["recent_exits"]
        )
        journal.state["strategy_states"] = states
    return target, states, marks
