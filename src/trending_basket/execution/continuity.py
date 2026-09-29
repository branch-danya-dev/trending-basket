"""Downtime, bounded exchange recovery and restart verification."""

from __future__ import annotations

import json
import random
import threading
from collections.abc import Callable
from decimal import Decimal as D

from trending_basket.execution import capital
from trending_basket.execution.journal import Halted, Journal
from trending_basket.execution.ledger import atomic_json, digest
from trending_basket.execution.live_executor import LiveExecutor
from trending_basket.execution.replay import fills as journal_fills

MINUTE = 60000
OVERLAP_MS = 5 * MINUTE
# Cancelled/rejected orders with no fills are retained for only 24 hours.
RECOVERY_MS = 86400000 - OVERLAP_MS


def count_downtime(journal: Journal) -> None:
    intervals = sorted(
        (r["start_ms"], r["end_ms"])
        for r in journal.read_rows("events")
        if r.get("kind") in {"downtime", "network_recovered"}
    )
    total, end = 0, 0
    for start, stop in intervals:
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    journal.state["downtime_ms"] = total


class Heartbeat:
    def __init__(self, journal: Journal, monotonic_ms: int) -> None:
        self.journal = journal
        self.previous_wall = journal.clock.now_ms()
        self.previous_mono = monotonic_ms
        self.last_write = 0
        self.counter = 0
        self.mutex = threading.Lock()
        self.pending_gaps: list[tuple[int, int]] = []
        self.done = threading.Event()
        self.worker: threading.Thread | None = None

    def startup(self) -> int:
        journal = self.journal
        path = journal.directory / "heartbeat"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        journal.state["restart_count"] = journal.state.get("restart_count", 0) + (
            1 if previous else 0
        )
        gap = self.previous_wall - previous["time_ms"] if previous else 0
        if previous is not None and gap > 2 * MINUTE:
            self.downtime(previous["time_ms"], self.previous_wall, "process_restart")
        journal.append(
            "events", kind="process_started", restart_count=journal.state["restart_count"]
        )
        journal.save()
        self.tick(self.previous_mono)
        return gap

    def downtime(self, start: int, end: int, reason: str) -> None:
        self.journal.append(
            "events",
            kind="downtime",
            start_ms=start,
            end_ms=end,
            duration_ms=end - start,
            reason=reason,
        )
        count_downtime(self.journal)
        self.journal.save()

    def observe(self, monotonic_ms: int) -> None:
        now = self.journal.clock.now_ms()
        wall_delta, mono_delta = now - self.previous_wall, monotonic_ms - self.previous_mono
        slept = wall_delta > 2 * MINUTE and abs(wall_delta - mono_delta) > 2 * MINUTE
        if slept:
            self.pending_gaps.append((self.previous_wall, now))
        if now - self.last_write >= MINUTE or self.last_write == 0:
            self.counter += 1
            atomic_json(
                self.journal.directory / "heartbeat",
                dict(time_ms=now, monotonic_ms=monotonic_ms, counter=self.counter),
            )
            self.last_write = now
        self.previous_wall, self.previous_mono = now, monotonic_ms

    def tick(self, monotonic_ms: int) -> bool:
        with self.mutex:
            self.observe(monotonic_ms)
            gaps, self.pending_gaps = self.pending_gaps, []
        for start, end in gaps:
            self.downtime(start, end, "sleep_or_clock_jump")
        return bool(gaps)

    def start_writer(self, monotonic: Callable[[], int]) -> None:
        def write() -> None:
            while not self.done.wait(30):
                with self.mutex:
                    self.observe(monotonic())

        self.worker = threading.Thread(target=write, daemon=True, name="demo-heartbeat")
        self.worker.start()

    def stop_writer(self) -> None:
        self.done.set()
        if self.worker:
            self.worker.join(timeout=2)


class Retry:
    def __init__(self) -> None:
        self.started_ms: int | None = None
        self.attempt = 0
        self.last_notice_ms = 0

    def failure(
        self, journal: Journal, now: int, *, jitter: float | None = None
    ) -> tuple[float, bool]:
        if self.started_ms is None:
            self.started_ms = int(journal.state.get("network_outage_start_ms", now))
            journal.state["network_outage_start_ms"] = self.started_ms
            journal.append("events", kind="network_outage", start_ms=now)
            journal.save()
        delay = min(
            300.0, 2 ** min(self.attempt, 8) * (1 + (random.random() if jitter is None else jitter))
        )
        self.attempt += 1
        notify = now - self.started_ms >= 3600000 and now - self.last_notice_ms >= 3600000
        if notify:
            self.last_notice_ms = now
        return delay, notify

    def success(self, journal: Journal, now: int) -> None:
        if self.started_ms is not None:
            journal.append(
                "events",
                kind="network_recovered",
                start_ms=self.started_ms,
                end_ms=now,
                duration_ms=now - self.started_ms,
            )
            count_downtime(journal)
            journal.state.pop("network_outage_start_ms", None)
            journal.save()
        self.started_ms, self.attempt, self.last_notice_ms = None, 0, 0


def recover(executor: LiveExecutor, *, accept_gap: bool = False) -> dict[str, int]:
    journal, client = executor.journal, executor.client
    state = journal.state
    now = client.now_ms()
    last = state.get("recovery_through_ms", state.get("snapshot_time_ms", now))
    if now - last > RECOVERY_MS:
        if not state.get("unrecoverable_gap"):
            gap = dict(start_ms=last, end_ms=now, maximum_ms=RECOVERY_MS)
            journal.append("events", kind="unrecoverable_gap", **gap)
            state["unrecoverable_gap"] = gap
            state["t006b_reset_ms"] = now
            journal.save()
        if not accept_gap:
            raise Halted("unrecoverable_gap; tb run verify --accept-gap requires manual review")
    if state.get("unrecoverable_gap") and not accept_gap:
        raise Halted("unrecoverable_gap requires tb run verify --accept-gap")
    start = max(state["capital"]["start_ms"], last - OVERLAP_MS, now - RECOVERY_MS)
    history = client.history(start, now)
    counts = dict(executions=0, orders=0, closed_pnl=0, transactions=0, stops=0)
    seen = state.setdefault("exchange_history", {})
    journal.recovered = True
    try:
        for channel, rows in history.items():
            keys = {
                "executions": "execId",
                "orders": "orderId",
                "closed_pnl": "orderId",
                "transactions": "id",
            }
            for row in rows:
                identity = str(row[keys[channel]])
                key = f"{channel}:{identity}"
                fingerprint = digest(row)
                if seen.get(key) == fingerprint:
                    continue
                # Order status can change; append the new revision under the same exchange ID.
                journal.append("exchange", kind=channel, exchange_id=identity, record=row)
                seen[key] = fingerprint
                counts[channel] += 1
        before = len(state.get("recent_exits", []))
        executor._record_fills(history["executions"])
        counts["stops"] = len(state.get("recent_exits", [])) - before
        known = {f["exec_id"]: f for f in journal_fills(journal)}
        for fill in history["executions"]:
            if fill.get("execType", "Trade") != "Trade":
                continue
            saved = known.get(fill["execId"])
            if saved is None:
                raise Halted("exchange execution missing from run journal")
            for local, remote in (
                ("quantity", "execQty"),
                ("execution_price", "execPrice"),
                ("fee_usd", "execFee"),
            ):
                if abs(D(str(saved[local])) - D(str(fill[remote]))) > D("1e-9"):
                    raise Halted(f"exchange execution differs from journal: {local}")
        for transaction in history["transactions"]:
            if transaction.get("type") != "TRADE":
                continue
            saved = state["capital"]["fills"].get(transaction.get("tradeId"))
            if saved is None:
                raise Halted("trade transaction missing from owned execution ledger")
            if abs(D(str(saved["fee_usd"])) - D(str(transaction["fee"]))) > D("1e-9") or abs(
                D(str(saved["realized_usd"])) - D(str(transaction["cashFlow"]))
            ) > D("1e-7"):
                raise Halted("exchange transaction cash flow or fee differs from owned fills")
        for pnl in history["closed_pnl"]:
            linked = [f for f in known.values() if f["order_id"] == pnl["orderId"]]
            if not linked:
                raise Halted("closed PnL has no owned closing execution")
            quantity = sum((D(f["quantity"]) for f in linked), D(0))
            if quantity != D(pnl["qty"]):
                raise Halted("closed PnL quantity differs from closing executions")
        # Overlapping transaction queries catch delayed funding without double charging fees.
        if accept_gap:
            state["last_poll_ms"] = now
            state["capital"]["funding_checked_ms"] = now
        capital.sync_funding(journal, client)
        actual = executor.reconcile()
        for symbol, position in actual.items():
            intended = D(
                str(
                    state.get("stop_intents", {}).get(
                        symbol, state["positions"][symbol]["stop_price"]
                    )
                )
            )
            if D(position.get("stopLoss") or "0") != intended:
                state["t006b_reset_ms"] = now
                journal.append("events", kind="manual_stop_change", symbol=symbol)
                journal.save()
                raise Halted("exchange stop differs from bot intent; manual review required")
        owned_ids = {r["order_id"] for r in journal.read_rows("fills")}
        owned_ids |= set(state["stop_ids"])
        for order in history["orders"]:
            if (
                order.get("orderLinkId") not in state["orders"]
                and order["orderId"] not in owned_ids
            ):
                state["t006b_reset_ms"] = now
                journal.save()
                raise Halted("unowned order in downtime history; manual action suspected")
        if accept_gap and state.get("unrecoverable_gap"):
            journal.append(
                "events",
                kind="gap_accepted",
                gap=state.pop("unrecoverable_gap"),
                accounting_complete=False,
            )
            state["accepted_gap"] = True
            state["t006b_reset_ms"] = now
        state["recovery_through_ms"] = now
        journal.append("events", kind="recovery_completed", start_ms=start, end_ms=now, **counts)
        journal.save()
    finally:
        journal.recovered = False
    return counts
