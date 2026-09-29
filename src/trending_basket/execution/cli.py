"""Demo preflight, daily execution, supervision and explicit halt recovery."""

from __future__ import annotations

import json
import time
from typing import Any

import typer
from pydantic import ValidationError

from trending_basket.clock import SystemClock
from trending_basket.config import Settings, load_settings
from trending_basket.execution.bybit_private import BybitPrivateClient, PrivateAPIError
from trending_basket.execution.cycle import (
    check_account,
    decision_day,
    drawdown_guard,
    risk_config,
    run_cycle,
    update_data,
)
from trending_basket.execution.journal import Halted, Journal, Notifier, PreviewJournal
from trending_basket.execution.live_executor import LiveExecutor
from trending_basket.execution.planning import ExchangeRules

run_app = typer.Typer(invoke_without_command=True, help="Execute the fixed Demo candidate.")
demo_app = typer.Typer(help="Read-only exchange preflight for Demo.")


def safe_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "invalid settings; check .env field names/types (values hidden)"
    return (
        str(exc) if isinstance(exc, (ValueError, Halted, PrivateAPIError)) else type(exc).__name__
    )


def demo_settings() -> Settings:
    settings = load_settings()
    if settings.mode != "demo":
        raise ValueError("TB_MODE=demo is required")
    return settings


def executor_for(client: BybitPrivateClient, journal: Journal, notifier: Notifier) -> LiveExecutor:
    rules = {s: ExchangeRules.from_api(client.instrument(s)) for s in journal.state["positions"]}
    pending = journal.state["pending_order"]
    if pending:
        symbol = pending["params"]["symbol"]
        rules[symbol] = ExchangeRules.from_api(client.instrument(symbol))
    return LiveExecutor(client, journal, notifier, rules)


def supervise(settings: Settings, executor: LiveExecutor) -> dict[str, Any]:
    executor.journal.require_running()
    account = check_account(executor.client, executor)
    _, threshold, _ = risk_config(settings.demo_risk_file)
    try:
        drawdown_guard(executor.journal, account["equity_usd"], threshold)
    except Halted as exc:
        executor.stop(str(exc))
    for symbol, position in list(executor.journal.state["positions"].items()):
        executor.confirm_stop(symbol, position["stop_price"])
        executor.ensure_liquidation_distance(
            symbol, position["average_entry_price"], decision_day(executor.client.now_ms())
        )
    return account


@run_app.callback()
def run(
    ctx: typer.Context,
    mode: str = "demo",
    once: bool = False,
    loop: bool = False,
    dry_run: bool = False,
) -> None:
    if ctx.invoked_subcommand:
        return
    if mode != "demo" or once == loop or (dry_run and loop):
        raise typer.BadParameter(
            "use --mode demo and exactly one of --once/--loop; --dry-run requires --once"
        )
    try:
        settings = demo_settings()
        clock = SystemClock()
        journal = Journal(settings.data_dir / "live" / "demo", clock)
        with journal.locked():
            if dry_run:
                journal = PreviewJournal(journal)
            notifier = Notifier(settings, journal)
            notifier.silent = dry_run
            client = BybitPrivateClient(settings, clock, on_error=journal.api_error)
            client.read_only = dry_run
            try:
                while True:
                    try:
                        journal.require_running()
                        client.sync_time()
                        executor = executor_for(client, journal, notifier)
                        if not dry_run:
                            if journal.state["pending_order"]:
                                executor.recover_order()
                            supervise(settings, executor)
                        if dry_run or journal.state["last_decision_ms"] != decision_day(
                            client.now_ms()
                        ):
                            typer.echo("Updating Demo market data...")
                            inputs = update_data(
                                settings,
                                decision_day(client.now_ms()),
                                set(journal.state["positions"]),
                                client,
                            )
                            result = run_cycle(
                                settings, client, journal, notifier, inputs, client, dry_run=dry_run
                            )
                            typer.echo(json.dumps(result, indent=2))
                        elif once:
                            typer.echo(
                                "Daily decision already completed; positions and stops checked."
                            )
                        if once:
                            break
                        time.sleep(10)
                    except PrivateAPIError as exc:
                        notifier.send(f"DEMO API error: code={exc.code}")
                        if exc.code == 10002:
                            journal.halt(
                                "exchange time is not synchronized; exchange stops preserved"
                            )
                        if journal.state["halt"]:
                            notifier.send(f"DEMO STOP: {journal.state['halt']['reason']}")
                        if journal.state["halt"] or once:
                            raise
                        time.sleep(2)
                    except Halted as exc:
                        if not dry_run and not journal.state["halt"]:
                            executor_for(client, journal, notifier).stop(str(exc))
                        raise
                    except Exception as exc:
                        if not dry_run:
                            journal.halt(f"Demo runtime error: {safe_error(exc)}")
                            notifier.send(f"DEMO STOP: {safe_error(exc)}; inspect before resume")
                        raise
            finally:
                client.close()
    except Exception as exc:
        # ValidationError / HTTP exception representations can contain secret input or URLs.
        safe = safe_error(exc)
        typer.echo(f"Demo stopped: {safe}", err=True)
        raise typer.Exit(1) from None


def resume_checkpoint(settings: Settings, executor: LiveExecutor) -> None:
    journal = executor.journal
    if not journal.state["halt"]:
        raise ValueError("Demo is not halted")
    # No order is resent by resume. Resolve an old intent read-only or cancel a known entry.
    check_account(executor.client, executor)
    if journal.state["pending_order"]:
        pending_order = journal.state["pending_order"]
        params = pending_order["params"]
        existing = executor.client.find_order(params["orderLinkId"], params["symbol"])
        if existing is None:
            # Explicit recovery after the user has reviewed the halt: never resend stale intent.
            journal.state["orders"][params["orderLinkId"]]["status"] = "AbandonedUnobserved"
            journal.state["pending_order"] = None
            journal.append("events", kind="intent_abandoned", order_link_id=params["orderLinkId"])
            journal.save()
        else:
            executor._cancel_pending()
    pending = journal.state["pending"]
    if pending and pending["day_ms"] != decision_day(executor.client.now_ms()):
        journal.append(
            "decisions",
            decision_ms=pending["day_ms"],
            status="missed_remainder",
            reason="explicit resume after interrupted day; no old targets executed",
        )
        journal.state["pending"] = None
    _, threshold, _ = risk_config(settings.demo_risk_file)
    drawdown_guard(journal, float(executor.client.wallet()["totalEquity"]), threshold)
    previous = journal.state["halt"]
    journal.state["halt"], journal.state["api_errors"] = None, 0
    journal.save()
    journal.append("events", kind="resumed", previous=previous)
    executor.notifier.send("DEMO resumed after console confirmation and reconciliation")


@run_app.command("resume")
def resume() -> None:
    try:
        settings = demo_settings()
        clock = SystemClock()
        journal = Journal(settings.data_dir / "live" / "demo", clock)
        with journal.locked():
            if not journal.state["halt"]:
                typer.echo("Demo is not halted.")
                return
            typer.echo(json.dumps(journal.state["halt"], indent=2))
            typer.confirm(
                "Reason investigated; resume after successful reconciliation?", abort=True
            )
            notifier = Notifier(settings, journal)
            client = BybitPrivateClient(settings, clock, on_error=journal.api_error)
            try:
                resume_checkpoint(settings, executor_for(client, journal, notifier))
            finally:
                client.close()
    except Exception as exc:
        typer.echo(
            "Resume refused: " + safe_error(exc),
            err=True,
        )
        raise typer.Exit(1) from None


@demo_app.command("check")
def check() -> None:
    try:
        settings = demo_settings()
        clock = SystemClock()
        journal = Journal(settings.data_dir / "live" / "demo", clock)
        with journal.locked():
            preview = PreviewJournal(journal)
            notifier = Notifier(settings, preview)
            client = BybitPrivateClient(settings, clock, on_error=preview.api_error)
            client.read_only = True
            try:
                account = check_account(client, executor_for(client, preview, notifier))
                inputs = update_data(
                    settings, decision_day(client.now_ms()), set(preview.state["positions"]), client
                )
                rules = {
                    s: ExchangeRules.from_api(client.instrument(s))
                    for s in inputs.universe.universe_at(decision_day(client.now_ms()))
                }
                telegram = notifier.send(
                    "trending-basket DEMO preflight: test notification, no orders submitted"
                )
                if not telegram:
                    raise ValueError("Telegram preflight failed")
                result = dict(
                    status="passed",
                    **account,
                    instruments=len(rules),
                    telegram=True,
                    private_mutations=0,
                )
                journal.append("events", kind="preflight", **result)
                typer.echo(json.dumps(result, indent=2))
            finally:
                client.close()
    except Exception as exc:
        typer.echo(
            "Preflight failed: " + safe_error(exc),
            err=True,
        )
        raise typer.Exit(1) from None
