"""Daily Demo decisions use the existing strategy, MarketView and portfolio limits."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pandas as pd

from trending_basket.backtest.config import Experiment, load_experiment
from trending_basket.backtest.inputs import Inputs, load_inputs
from trending_basket.backtest.periods import WARMUP_MS
from trending_basket.backtest.sim_executor import Position, SimExecutor
from trending_basket.clock import Clock
from trending_basket.config import Settings
from trending_basket.data.bybit_client import BybitPublicClient, build_client
from trending_basket.data.cache import sync_funding, sync_instruments, sync_klines
from trending_basket.domain.types import Interval
from trending_basket.execution.bybit_private import DEMO_URL, BybitPrivateClient
from trending_basket.execution.journal import Halted, Journal, Notifier, PreviewJournal
from trending_basket.execution.live_executor import LiveExecutor
from trending_basket.execution.planning import ExchangeRules, planned_delta, protected_stop
from trending_basket.portfolio.limits import apply_limits
from trending_basket.strategies.base import (
    Decision,
    DecisionContext,
    PositionExit,
    PositionView,
    TargetPosition,
)
from trending_basket.strategies.trend_basket import SymbolState, TrendBasket, TrendBasketParams
from trending_basket.universe.candidates import candidate_pool
from trending_basket.universe.selection import SelectionParameters
from trending_basket.universe.storage import (
    build_universe,
    load_universe,
    save_universe,
    universe_paths,
)

DAY_MS = 86400000
DELAY_MS = 180000


def decision_day(now_ms: int) -> int:
    return (now_ms - DELAY_MS) // DAY_MS * DAY_MS


def risk_config(path: Path) -> tuple[Experiment, float, str]:
    raw = path.read_bytes()
    document = json.loads(raw)
    if document["rule"] != "ADR-018" or document["candidate"] != "V1x":
        raise ValueError("registered T006 risk decision required")
    risk = float(document["risk_per_symbol_frac"])
    if not 0 < risk <= 0.01:
        raise ValueError("risk exceeds registered cap")
    base_path = Path("experiments/V1x.toml")
    base = load_experiment(base_path)
    config = Experiment.model_validate(
        base.model_dump()
        | {
            "strategy_params": base.strategy_params | {"risk_per_symbol_frac": risk},
            "execution": {"quantity_mode": "exchange"},
        }
    )
    registered = Experiment.model_validate(document["runs"]["dev_exact"]["resolved_config"])
    if (
        config.strategy_params != registered.strategy_params
        or config.limits != registered.limits
        or config.costs != registered.costs
        or config.run.interval != Interval.D1
        or config.run.strategy != "trend_basket"
    ):
        raise ValueError("Demo candidate differs from the registered V1x risk run")
    threshold = max(
        0.15,
        1.5
        * max(
            abs(document["runs"][p]["metrics"]["max_drawdown_frac"])
            for p in ("dev_exact", "val_exact")
        ),
    )
    if abs(float(document["stop_drawdown_frac"]) - threshold) > 1e-12:
        raise ValueError("Demo drawdown threshold differs from the registered rule")
    return config, threshold, hashlib.sha256(raw + base_path.read_bytes()).hexdigest()


def decide(
    config: Experiment, inputs: Inputs, day_ms: int, equity_usd: float, state: dict[str, Any]
) -> tuple[Decision, dict[str, Any], dict[str, float]]:
    strategy = TrendBasket(TrendBasketParams.model_validate(config.strategy_params), Interval.D1)
    strategy.states = {s: SymbolState(**value) for s, value in state["strategy_states"].items()}
    universe = tuple(
        s for s in inputs.universe.universe_at(day_ms) if inputs.universe.is_tradeable_at(s, day_ms)
    )
    symbols = sorted(set(universe) | set(state["positions"]))
    marks = {s: inputs.data.close_price(s, day_ms) for s in symbols}
    positions = {
        s: PositionView(
            float(p["quantity"]),
            p["average_entry_price"],
            p["stop_price"],
            p.get("initial_risk_usd"),
            float(p["quantity"]) * marks[s],
            p["entry_time_ms"],
            p["lifecycle_id"],
        )
        for s, p in state["positions"].items()
    }
    context = DecisionContext(
        day_ms,
        equity_usd,
        positions,
        universe,
        inputs.data.view(day_ms, symbols),
        tuple(PositionExit(**e) for e in state.get("recent_exits", [])),
    )
    targets = strategy.decide(context)
    for symbol in positions:
        if not inputs.universe.is_tradeable_at(symbol, day_ms):
            targets[symbol] = TargetPosition(0)
    limited, _ = apply_limits(targets, equity_usd, config.limits)
    return limited, {s: asdict(value) for s, value in strategy.states.items()}, marks


def update_data(settings: Settings, day_ms: int, held: set[str], clock: Clock) -> Inputs:
    public = build_client(settings.model_copy(update={"bybit_rest_url": DEMO_URL}), clock=clock)
    try:
        return _update_data(settings, day_ms, held, clock, public)
    finally:
        public.close()


def _update_data(
    settings: Settings, day_ms: int, held: set[str], clock: Clock, public: BybitPublicClient
) -> Inputs:
    sync_instruments(data_dir=settings.data_dir, client=public, clock=clock)
    name = "demo-core15"
    path, _ = universe_paths(settings.data_dir, name)
    if not path.exists():
        universe = load_universe(settings.data_dir, "core15")
        universe.metadata = copy.deepcopy(universe.metadata)
        universe.metadata["name"] = name
        save_universe(settings.data_dir, name, universe)
    universe = load_universe(settings.data_dir, name)
    date = datetime.fromtimestamp(day_ms / 1000, UTC)
    month_ms = int(
        date.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    if max(universe.metadata["rebalance_times_ms"]) < month_ms:
        for symbol in candidate_pool(settings.data_dir).symbols:
            sync_klines(
                data_dir=settings.data_dir,
                client=public,
                symbol=symbol,
                interval=Interval.D1,
                since_ms=WARMUP_MS,
            )
        new = build_universe(
            data_dir=settings.data_dir,
            name=name,
            since_ms=month_ms,
            parameters=SelectionParameters(),
            clock=clock,
        )
        new.table = pd.concat(
            [universe.table[universe.table.rebalance_time_ms < month_ms], new.table],
            ignore_index=True,
        )
        new.metadata["rebalance_times_ms"] = sorted(
            set(universe.metadata["rebalance_times_ms"]) | set(new.metadata["rebalance_times_ms"])
        )
        new.metadata["trading_periods"] = (
            universe.metadata["trading_periods"] | new.metadata["trading_periods"]
        )
        save_universe(settings.data_dir, name, new)
        universe = new
    for symbol in sorted(set(universe.universe_at(day_ms)) | held | {"BTCUSDT"}):
        for interval in Interval:
            sync_klines(
                data_dir=settings.data_dir,
                client=public,
                symbol=symbol,
                interval=interval,
                since_ms=WARMUP_MS,
            )
        sync_funding(data_dir=settings.data_dir, client=public, symbol=symbol, since_ms=WARMUP_MS)
    config, _, _ = risk_config(settings.demo_risk_file)
    # MarketView below still slices at day_ms; intraday cached data cannot reach the strategy.
    run = config.run.model_copy(
        update=dict(universe=name, period=None, start=date.date(), end=date.date())
    )
    inputs = load_inputs(settings.data_dir, config.model_copy(update={"run": run}))
    for symbol in universe.universe_at(day_ms):
        bar = inputs.data.last_closed(symbol, Interval.D1, day_ms)
        if bar is None or bar.open_time_ms != day_ms - DAY_MS:
            raise ValueError(f"stale daily cache: {symbol}")
    return inputs


def drawdown_guard(journal: Journal, equity_usd: float, threshold: float) -> None:
    peak = max(journal.state["peak_equity_usd"], equity_usd)
    journal.state["peak_equity_usd"] = peak
    journal.save()
    journal.append("equity", equity_usd=equity_usd, peak_equity_usd=peak)
    if equity_usd <= 0 or (peak - equity_usd) / peak > threshold:
        raise Halted("drawdown from peak exceeds registered limit")


def shadow(
    config: Experiment,
    targets: Decision,
    marks: dict[str, float],
    state: dict[str, Any],
    rules: dict[str, ExchangeRules],
    equity_usd: float,
    day_ms: int,
) -> list[dict[str, Any]]:
    book = SimExecutor(equity_usd, {s: r.quantity for s, r in rules.items()}, config.costs)
    for symbol, p in state["positions"].items():
        book.positions[symbol] = Position(
            p["lifecycle_id"],
            symbol,
            float(p["quantity"]),
            p["average_entry_price"],
            marks[symbol],
            p["entry_time_ms"],
            p["stop_price"],
            p.get("initial_risk_usd"),
        )
        book.cash_usd -= float(p["quantity"]) * marks[symbol]
    ordered = sorted(
        targets,
        key=lambda s: (
            targets[s].notional_usd
            > float(state["positions"].get(s, {}).get("quantity", 0)) * marks[s],
            s,
        ),
    )
    for symbol in ordered:
        book.rebalance(
            symbol,
            targets[symbol],
            marks[symbol],
            marks[symbol],
            day_ms,
            allow_increase=targets[symbol].allow_increase,
        )
    book.enforce_limits(marks, day_ms, config.limits)
    return book.fills


def check_account(client: BybitPrivateClient, executor: LiveExecutor) -> dict[str, Any]:
    client.sync_time()
    account = client.account()
    if account.get("marginMode") == "PORTFOLIO_MARGIN":
        raise ValueError("portfolio margin does not expose the required liquidation price")
    wallet = client.wallet()
    if wallet["accountType"] != "UNIFIED" or float(wallet["totalEquity"]) <= 0:
        raise ValueError("positive Unified Demo equity required")
    permissions = client.key_permissions()
    groups = permissions.get("permissions") or {}
    flat = [str(item) for value in groups.values() for item in value]
    if permissions.get("readOnly") != 0 or any("withdraw" in item.lower() for item in flat):
        raise ValueError("key must be writable and have no withdrawal permission")
    if not (
        {"Order", "Position"} <= set(groups.get("ContractTrade", []))
        or "DerivativesTrade" in groups.get("Derivatives", [])
    ):
        raise ValueError("contract trading permissions missing")
    if not permissions.get("ips") or "*" in permissions["ips"]:
        raise ValueError("IP-restricted API key required by AGENTS.md")
    executor.reconcile()
    return dict(
        account_type=wallet["accountType"],
        margin_mode=account.get("marginMode"),
        equity_usd=float(wallet["totalEquity"]),
        clock_offset_ms=client.offset_ms,
        key_permissions_checked=True,
    )


def run_cycle(
    settings: Settings,
    client: BybitPrivateClient,
    journal: Journal,
    notifier: Notifier,
    inputs: Inputs,
    clock: Clock,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run and not isinstance(journal, PreviewJournal):
        preview_journal = PreviewJournal(journal)
        preview_notifier = Notifier(settings, preview_journal)
        preview_notifier.silent = True
        previous_read_only, previous_error = client.read_only, client.on_error
        client.read_only, client.on_error = True, preview_journal.api_error
        try:
            return run_cycle(
                settings, client, preview_journal, preview_notifier, inputs, clock, dry_run=True
            )
        finally:
            client.read_only, client.on_error = previous_read_only, previous_error
    config, threshold, config_sha = risk_config(settings.demo_risk_file)
    state = journal.state
    journal.require_running()
    if state.get("config_sha256", config_sha) != config_sha:
        raise Halted("risk configuration changed during Demo")
    state["config_sha256"] = config_sha
    day = decision_day(clock.now_ms())
    symbols = sorted(set(inputs.universe.universe_at(day)) | set(state["positions"]))
    for symbol in symbols:
        bar = inputs.data.last_closed(symbol, Interval.D1, day)
        if inputs.universe.is_tradeable_at(symbol, day) and (
            bar is None or bar.open_time_ms != day - DAY_MS
        ):
            raise ValueError(f"stale daily cache: {symbol}")
    rules = {s: ExchangeRules.from_api(client.instrument(s)) for s in symbols}
    executor = LiveExecutor(client, journal, notifier, rules)
    if state["pending_order"] and not dry_run:
        executor.recover_order()
    account = check_account(client, executor)
    drawdown_guard(journal, account["equity_usd"], threshold)
    # Even an already completed day must check existing exchange protection.
    if not dry_run:
        for symbol, p in list(state["positions"].items()):
            executor.confirm_stop(symbol, p["stop_price"])
            executor.ensure_liquidation_distance(symbol, p["average_entry_price"], day)
    if state["last_decision_ms"] == day:
        return dict(status="already_completed", decision_ms=day)
    if state["pending"] and state["pending"]["day_ms"] != day:
        raise Halted("unfinished previous-day decision; reconcile and resume explicitly")
    if state["pending"]:
        pending = state["pending"]
        targets = {s: TargetPosition(**t) for s, t in pending["targets"].items()}
        strategy_states, marks = pending["strategy_states"], pending["marks"]
    else:
        targets, strategy_states, marks = decide(config, inputs, day, account["equity_usd"], state)
        pending = dict(
            day_ms=day,
            targets={s: asdict(t) for s, t in targets.items()},
            strategy_states=strategy_states,
            marks=marks,
            completed_symbols=[],
            consumed_exits=len(state.get("recent_exits", [])),
            shadow_fills=shadow(config, targets, marks, state, rules, account["equity_usd"], day),
        )
    quotes = {s: float(client.ticker(s)["lastPrice"]) for s in targets}
    plan = []
    for symbol, target in targets.items():
        held = D(state["positions"].get(symbol, {}).get("quantity", "0"))
        delta, reason = planned_delta(
            target.notional_usd,
            held,
            D(str(quotes[symbol])),
            rules[symbol],
            allow_increase=symbol in inputs.universe.universe_at(day) and target.allow_increase,
        )
        stop = (
            str(
                protected_stop(
                    target.stop_price,
                    D(str(state["positions"].get(symbol, {}).get("stop_price", 0))),
                    rules[symbol].tick_size,
                )
            )
            if target.stop_price
            else None
        )
        plan.append(
            dict(
                symbol=symbol,
                delta=str(delta),
                reduce_only=delta < 0,
                stop=stop,
                reason=reason,
                decision_price=marks[symbol],
                current_price=quotes[symbol],
            )
        )
    preview = dict(
        status="dry_run" if dry_run else "completed",
        decision_ms=day,
        late=clock.now_ms() > day + DELAY_MS + 1000,
        equity_usd=account["equity_usd"],
        plan=plan,
    )
    if dry_run:
        return preview
    if not state["pending"]:
        previous = state["last_decision_ms"]
        if previous is not None:
            for missed in range(previous + DAY_MS, day, DAY_MS):
                journal.append(
                    "decisions",
                    decision_ms=missed,
                    status="missed",
                    reason="process offline; latest closed candle only",
                )
        state["pending"] = pending
        journal.save()
        journal.append("decisions", **(preview | {"status": "started"}))
    # Persisted with the decision, so a restart cannot lose or recompute its shadow baseline.
    journal.append("shadow", decision_ms=day, fills=pending["shadow_fills"])
    for item in sorted(plan, key=lambda p: (not p["reduce_only"], p["symbol"])):
        symbol = item["symbol"]
        if symbol in pending["completed_symbols"]:
            continue
        executor.rebalance(
            symbol,
            targets[symbol],
            marks[symbol],
            quotes[symbol],
            day,
            allow_increase=symbol in inputs.universe.universe_at(day)
            and targets[symbol].allow_increase,
        )
        if symbol in state["positions"]:
            state["positions"][symbol]["decision_price"] = marks[symbol]
        pending["completed_symbols"].append(symbol)
        journal.save()
    executor.enforce_limits(marks, day, config.limits)
    executor.reconcile()
    record_comparison(journal, day)
    state["strategy_states"] = strategy_states
    state["recent_exits"] = state.get("recent_exits", [])[pending["consumed_exits"] :]
    state["last_decision_ms"], state["pending"] = day, None
    state["api_errors"] = 0
    journal.save()
    journal.append("decisions", **preview)
    actual = executor.actual()
    position_summary = {s: dict(qty=p["size"], stop=p["stopLoss"]) for s, p in actual.items()}
    notifier.send(
        f"DEMO daily summary: equity={client.wallet()['totalEquity']}; "
        f"positions={json.dumps(position_summary)}; "
        f"orders={sum(o['decision_ms'] == day for o in state['orders'].values())}"
    )
    return preview


def record_comparison(journal: Journal, day_ms: int) -> None:
    def rows(channel: str) -> list[dict[str, Any]]:
        path = journal.directory / f"{channel}.jsonl"
        return (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if path.exists()
            else []
        )

    simulated = [r for r in rows("shadow") if r.get("decision_ms") == day_ms and "fills" in r]
    actual = {
        r["exec_id"]: r
        for r in rows("fills")
        if journal.state["orders"].get(r["order_link_id"], {}).get("decision_ms") == day_ms
    }
    simulated_fills = simulated[-1]["fills"] if simulated else []
    differences = []
    for symbol in sorted(
        {f["symbol"] for f in simulated_fills} | {f["symbol"] for f in actual.values()}
    ):
        for side in ("buy", "sell"):
            expected = [f for f in simulated_fills if f["symbol"] == symbol and f["side"] == side]
            observed = [
                f for f in actual.values() if f["symbol"] == symbol and f["side"].lower() == side
            ]
            sq = sum(float(f["quantity"]) for f in expected)
            aq = sum(float(f["quantity"]) for f in observed)
            sp = sum(float(f["quantity"]) * f["price"] for f in expected) / sq if sq else None
            ap = (
                sum(float(f["quantity"]) * float(f["execution_price"]) for f in observed) / aq
                if aq
                else None
            )
            differences.append(
                dict(
                    symbol=symbol,
                    side=side,
                    quantity_delta=aq - sq,
                    fee_delta_usd=sum(f["fee_usd"] for f in observed)
                    - sum(f["fee_usd"] for f in expected),
                    execution_price_delta=None if sp is None or ap is None else ap - sp,
                )
            )
    journal.append(
        "shadow",
        kind="comparison",
        decision_ms=day_ms,
        simulated_fills=simulated_fills,
        actual_fills=list(actual.values()),
        differences=differences,
    )
