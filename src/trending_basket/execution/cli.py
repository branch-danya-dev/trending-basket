"""Demo preflight, daily execution, supervision and explicit halt recovery."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import typer
from pydantic import ValidationError

from trending_basket.clock import SystemClock
from trending_basket.config import Settings, load_settings
from trending_basket.data.bybit_client import BybitAPIError
from trending_basket.execution.bybit_private import BybitPrivateClient, PrivateAPIError
from trending_basket.execution.continuity import Heartbeat, Retry, recover
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
from trending_basket.execution.messages import reason_ru
from trending_basket.execution.planning import ExchangeRules
from trending_basket.execution.replay import reconstruct_equity, replay_strategy, verify_capital
from trending_basket.execution.runs import active_directory
from trending_basket.execution.runs import close as close_run
from trending_basket.execution.runs import create as create_run
from trending_basket.execution.runs import root as run_root
from trending_basket.execution.runs import validate as validate_run

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
    if settings.allocated_capital_usd is None:
        raise ValueError("TB_ALLOCATED_CAPITAL_USD is required")
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


def held_symbols(journal: Journal) -> set[str]:
    return (
        set(journal.state["positions"])
        | set(journal.state.get("strategy_states", {}))
        | set(journal.state.get("shadow_book", {}).get("positions", {}))
        | set(journal.state.get("protected_reentries", {}))
    )


def transient(exc: Exception) -> bool:
    return (
        (isinstance(exc, PrivateAPIError) and exc.code in {-1, 10000, 10006, 10016})
        or (isinstance(exc, BybitAPIError) and exc.ret_code in {-1, 10006})
        or isinstance(exc, httpx.TransportError)
    )


def verify_current(
    settings: Settings,
    client: BybitPrivateClient,
    journal: Journal,
    notifier: Notifier,
    *,
    accept_gap: bool = False,
) -> dict[str, Any]:
    executor = executor_for(client, journal, notifier)
    counts = recover(executor, accept_gap=accept_gap)
    inputs = update_data(settings, decision_day(client.now_ms()), held_symbols(journal), client)
    config, _, _ = risk_config(settings.demo_risk_file)
    replay_strategy(journal, config, inputs)
    values = verify_capital(journal, executor.actual())
    reconstruct_equity(journal, inputs, client.now_ms())
    # Validation observes protection; it never places orders or repairs stops silently.
    for symbol, position in executor.actual().items():
        price = Decimal(str(journal.state["positions"][symbol]["stop_price"]))
        if not any(executor._valid_stop(o, position, price) for o in client.open_orders()):
            raise Halted("exchange stop not confirmed during verify")
    journal.append("events", kind="run_verified", counts=counts, **values)
    journal.save()
    return dict(
        status="consistent",
        run_id=journal.state["run_id"],
        recovered=counts,
        allocated_capital=values,
        accepted_gap=journal.state.get("accepted_gap", False),
        interruptions=[
            r
            for r in journal.read_rows("events")
            if r.get("kind")
            in {
                "downtime",
                "network_outage",
                "network_recovered",
                "recovery_completed",
                "unrecoverable_gap",
                "gap_accepted",
            }
        ],
    )


@run_app.callback()
def run(
    ctx: typer.Context,
    mode: str = "demo",
    once: bool = False,
    loop: bool = False,
    dry_run: bool = False,
    allow_code_change: bool = False,
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
        journal = Journal(active_directory(settings), clock)
        with journal.locked():
            if dry_run:
                journal = PreviewJournal(journal)
            notifier = Notifier(settings, journal)
            notifier.silent = dry_run
            client = BybitPrivateClient(settings, clock, on_error=journal.api_error)
            heartbeat = Heartbeat(journal, clock.monotonic_ms()) if not dry_run else None
            gap = heartbeat.startup() if heartbeat else 0
            retry = Retry()
            retry.started_ms = journal.state.get("network_outage_start_ms")
            if heartbeat:
                heartbeat.start_writer(clock.monotonic_ms)
            initialized = False
            try:
                while True:
                    try:
                        if heartbeat and heartbeat.tick(clock.monotonic_ms()):
                            initialized = False
                        client.read_only = True
                        client.sync_time()
                        if not initialized:
                            validate_run(
                                journal, settings, client, allow_code_change=allow_code_change
                            )
                            verified = verify_current(settings, client, journal, notifier)
                            if gap > 120000 or retry.started_ms is not None:
                                counts = verified["recovered"]
                                downtime = max(
                                    gap, clock.now_ms() - (retry.started_ms or clock.now_ms())
                                )
                                late = journal.state["last_decision_ms"] != decision_day(
                                    client.now_ms()
                                )
                                notifier.send(
                                    "Demo: восстановление после простоя "
                                    f"{downtime / 60000:.1f} мин. "
                                    f"Исполнений: {counts['executions']}. "
                                    f"Стопов: {counts['stops']}. "
                                    f"Транзакций: {counts['transactions']}. "
                                    "Сверка пройдена. "
                                    f"Запоздалое решение: {'да' if late else 'нет'}. "
                                    "Прогон активен."
                                )
                            initialized, gap = True, 0
                        journal.require_running()
                        client.read_only = dry_run
                        executor = executor_for(client, journal, notifier)
                        if not dry_run:
                            if (
                                client.now_ms() - journal.state.get("recovery_through_ms", 0)
                                >= 60000
                            ):
                                client.read_only = True
                                recover(executor)
                                client.read_only = False
                            if journal.state["pending_order"]:
                                executor.recover_order()
                            supervise(settings, executor)
                        if dry_run or journal.state["last_decision_ms"] != decision_day(
                            client.now_ms()
                        ):
                            inputs = update_data(
                                settings,
                                decision_day(client.now_ms()),
                                held_symbols(journal),
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
                        retry.success(journal, clock.now_ms())
                        if once:
                            break
                        client.sleep(10)
                    except Exception as exc:
                        if transient(exc) and loop:
                            initialized = False
                            delay, notice = retry.failure(journal, clock.now_ms())
                            if notice:
                                notifier.send(
                                    "Demo: сеть недоступна больше часа; повторяем соединение. "
                                    "Биржевые стопы сохранены."
                                )
                            # Heartbeat remains active even during the maximum five-minute backoff.
                            while delay > 0:
                                pause = min(30, delay)
                                client.sleep(pause)
                                delay -= pause
                                if heartbeat:
                                    heartbeat.tick(clock.monotonic_ms())
                            continue
                        code_refusal = "require --allow-code-change" in str(exc)
                        if not dry_run and not transient(exc) and not code_refusal:
                            journal.halt(safe_error(exc))
                            notifier.send(
                                f"Остановка Demo: {reason_ru(safe_error(exc))}. "
                                "Биржевые стопы сохранены."
                            )
                        raise
            finally:
                if heartbeat:
                    heartbeat.stop_writer()
                client.close()
    except Exception as exc:
        typer.echo(f"Demo stopped: {safe_error(exc)}", err=True)
        raise typer.Exit(1) from None


def resume_checkpoint(settings: Settings, executor: LiveExecutor) -> None:
    journal = executor.journal
    if not journal.state["halt"]:
        raise ValueError("Demo is not halted")
    # No order is resent by resume. Resolve an old intent read-only or cancel a known entry.
    account = check_account(executor.client, executor)
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
    drawdown_guard(journal, account["equity_usd"], threshold)
    previous = journal.state["halt"]
    journal.state["halt"], journal.state["api_errors"] = None, 0
    journal.save()
    journal.append("events", kind="resumed", previous=previous)
    executor.notifier.send(
        "Demo возобновлён после подтверждения в консоли и успешной сверки с биржей."
    )


@run_app.command("resume")
def resume() -> None:
    try:
        settings = demo_settings()
        clock = SystemClock()
        journal = Journal(active_directory(settings), clock)
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
                client.read_only = True
                validate_run(journal, settings, client)
                verify_current(settings, client, journal, notifier)
                client.read_only = False
                resume_checkpoint(settings, executor_for(client, journal, notifier))
            finally:
                client.close()
    except Exception as exc:
        typer.echo(
            "Resume refused: " + safe_error(exc),
            err=True,
        )
        raise typer.Exit(1) from None


@run_app.command("start")
def start(name: str = "demo-001", adopt_legacy: bool = False) -> None:
    """Create an explicit run; adopting the existing T006a book requires a flag."""
    from trending_basket.execution import capital
    from trending_basket.execution.cycle import decide
    from trending_basket.execution.ledger import atomic_json

    try:
        settings, clock = demo_settings(), SystemClock()
        original = Journal(run_root(settings), clock)
        with original.locked():
            used = (run_root(settings) / "legacy_adopted.json").exists()
            has_legacy = bool(original.state["orders"]) and not used
            if has_legacy and not adopt_legacy:
                raise Halted("existing T006a book found; use --adopt-legacy to preserve it")
            if adopt_legacy and (
                not has_legacy or original.state["pending"] or original.state["pending_order"]
            ):
                raise Halted("legacy adoption requires one unreconciled-free existing book")
            preview = PreviewJournal(
                original if has_legacy else Journal(run_root(settings) / "preflight", clock)
            )
            notifier = Notifier(settings, preview)
            notifier.silent = True
            client = BybitPrivateClient(settings, clock, on_error=preview.api_error)
            client.read_only = True
            try:
                check_account(client, executor_for(client, preview, notifier))
                bootstrap = None
                if has_legacy:
                    rows = [
                        r
                        for r in original.read_rows("decisions")
                        if r.get("status") == "completed"
                        and r["time_ms"] >= preview.state["capital"]["start_ms"]
                    ]
                    if len(rows) != 1:
                        raise Halted(
                            "legacy adoption requires exactly one allocated-capital decision"
                        )
                    inputs = update_data(
                        settings, decision_day(client.now_ms()), held_symbols(preview), client
                    )
                    config, _, _ = risk_config(settings.demo_risk_file)
                    context: dict[str, Any] = dict(
                        positions={}, strategy_states={}, recent_exits=[]
                    )
                    _, states, _ = decide(
                        config, inputs, rows[0]["decision_ms"], rows[0]["equity_usd"], context
                    )
                    if states != preview.state["strategy_states"]:
                        raise Halted("legacy strategy differs from independent candle replay")
                    bootstrap = dict(
                        day_ms=rows[0]["decision_ms"],
                        equity_usd=rows[0]["equity_usd"],
                        context=context,
                        result=states,
                    )
                journal = create_run(
                    settings, clock, client, name, legacy=preview if has_legacy else None
                )
                if has_legacy:
                    journal.state["recovery_through_ms"] = (
                        original.state.get("last_poll_ms") or original.state["capital"]["start_ms"]
                    )
                capital.initialize(journal, settings.allocated_capital_usd, client.now_ms())
                if bootstrap:
                    journal.append("strategy", **bootstrap)
                    assert journal.ledger is not None
                    journal.state["strategy_steps"] = [journal.ledger.records[-1]["seq"]]
                    journal.state["t006b_started_ms"] = rows[0]["time_ms"]
                    atomic_json(
                        run_root(settings) / "legacy_adopted.json",
                        {"run_id": journal.state["run_id"]},
                    )
                journal.save()
                typer.echo(f"Active run: {journal.state['run_id']}; no exchange mutations.")
            finally:
                client.close()
    except Exception as exc:
        typer.echo("Start refused: " + safe_error(exc), err=True)
        raise typer.Exit(1) from None


@run_app.command("verify")
def verify(accept_gap: bool = False, allow_code_change: bool = False) -> None:
    """Verify journal, recovered exchange book and independent strategy replay."""
    try:
        settings, clock = demo_settings(), SystemClock()
        journal = Journal(active_directory(settings), clock)
        with journal.locked():
            if accept_gap:
                typer.confirm(
                    "Manually reviewed the gap; accept incomplete history and restart T006b count?",
                    abort=True,
                )
            notifier = Notifier(settings, journal)
            notifier.silent = True
            client = BybitPrivateClient(settings, clock, on_error=journal.api_error)
            client.read_only = True
            try:
                validate_run(journal, settings, client, allow_code_change=allow_code_change)
                result = verify_current(settings, client, journal, notifier, accept_gap=accept_gap)
                if accept_gap and (journal.state.get("halt") or {}).get("reason", "").startswith(
                    "unrecoverable_gap"
                ):
                    journal.state["halt"] = None
                    journal.save()
                typer.echo(json.dumps(result, indent=2))
            finally:
                client.close()
    except Exception as exc:
        typer.echo("Verify failed: " + safe_error(exc), err=True)
        raise typer.Exit(1) from None


@run_app.command("close")
def close() -> None:
    """Archive the run only when no exchange exposure or pending intent remains."""
    try:
        settings, clock = demo_settings(), SystemClock()
        journal = Journal(active_directory(settings), clock)
        with journal.locked():
            client = BybitPrivateClient(settings, clock, on_error=journal.api_error)
            client.read_only = True
            try:
                notifier = Notifier(settings, journal)
                notifier.silent = True
                # A changed config may be closed; no strategy/order uses the new settings.
                executor_for(client, journal, notifier).reconcile()
                close_run(journal, settings)
                typer.echo("Run closed; exchange orders were not changed.")
            finally:
                client.close()
    except Exception as exc:
        typer.echo("Close refused: " + safe_error(exc), err=True)
        raise typer.Exit(1) from None


@demo_app.command("check")
def check() -> None:
    try:
        settings = demo_settings()
        clock = SystemClock()
        journal = Journal(
            active_directory(settings)
            if (run_root(settings) / "active_run").exists()
            else run_root(settings),
            clock,
        )
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
                    "Demo: проверка уведомлений пройдена; заявки не отправлялись."
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
