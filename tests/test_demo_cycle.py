"""Daily checkpointing, shadow and strategy parity independent of external services."""

import copy
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from backtest_support import DAY, START, candle, store, universe
from demo_support import SYMBOL, mutations, setup_demo
from typer.testing import CliRunner

from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.inputs import Inputs
from trending_basket.cli import app
from trending_basket.execution.cycle import decide, risk_config, run_cycle
from trending_basket.execution.journal import Halted
from trending_basket.portfolio.limits import apply_limits
from trending_basket.strategies.trend_basket import TrendBasket, TrendBasketParams


def market(executor):
    rows = [
        candle(i, open=100 + i * 0.2, close=100 + i * 0.2, high=100 + i * 0.2, low=99.8 + i * 0.2)
        for i in range(405)
    ]
    return Inputs(
        store(rows), universe(), {SYMBOL: executor.rules[SYMBOL].quantity}, {SYMBOL: {}}, []
    )


def test_late_cycle_once_dry_run_does_not_consume_state_and_shadow_is_recorded(tmp_path):
    settings, clock, api, journal, client, executor = setup_demo(tmp_path)
    inputs = market(executor)
    day = START + 400 * DAY
    clock.set(day + 5 * 3600000)
    api.last_price = str(inputs.data.close_price(SYMBOL, day))
    journal.state["last_decision_ms"] = day - 3 * DAY
    journal.save()
    before = journal.path.read_bytes()
    result = run_cycle(settings, client, journal, executor.notifier, inputs, clock, dry_run=True)
    assert result["status"] == "dry_run" and result["late"]
    assert any(float(row["delta"]) > 0 for row in result["plan"])
    assert not mutations(api) and journal.path.read_bytes() == before
    result = run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    assert result["status"] == "completed" and result["late"]
    created = len(mutations(api, "/v5/order/create"))
    assert created > 0
    again = run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    assert again["status"] == "already_completed"
    assert len(mutations(api, "/v5/order/create")) == created
    decisions = [
        json.loads(line)
        for line in (journal.directory / "decisions.jsonl").read_text().splitlines()
    ]
    assert [r["decision_ms"] for r in decisions if r["status"] == "missed"] == [
        day - 2 * DAY,
        day - DAY,
    ]
    shadow = [
        json.loads(line) for line in (journal.directory / "shadow.jsonl").read_text().splitlines()
    ]
    assert shadow[0]["fills"] and shadow[-1]["actual_fills"]
    assert shadow[-1]["simulated_fills"] == shadow[0]["fills"]


def test_demo_historical_decisions_equal_engine_with_same_capital_and_positions(tmp_path):
    settings, _, _, journal, _, executor = setup_demo(tmp_path)
    inputs = market(executor)
    config, _, _ = risk_config(settings.demo_risk_file)
    day = START + 400 * DAY
    config = config.model_copy(
        update={
            "run": config.run.model_copy(
                update={
                    "period": None,
                    "start": datetime.fromtimestamp(day / 1000, UTC).date(),
                    "end": datetime.fromtimestamp(day / 1000, UTC).date(),
                    "initial_capital_usd": 1000,
                }
            )
        }
    )
    engine = BacktestEngine(config, inputs.data, inputs.universe, inputs.rules, inputs.funding)

    class RecordingStrategy(TrendBasket):
        def decide(self, ctx):
            self.last_context = ctx
            self.last_requested = super().decide(ctx)
            return self.last_requested

    strategy = RecordingStrategy(TrendBasketParams.model_validate(config.strategy_params))
    # Two consecutive boundaries verify both empty-account initialization and carried state.
    for at in (day, day + DAY):
        state = copy.deepcopy(journal.state)
        state["strategy_states"] = {s: asdict(v) for s, v in strategy.states.items()}
        state["positions"] = {
            s: asdict(p) | {"quantity": str(p.quantity)} for s, p in engine.book.positions.items()
        }
        marks = {s: inputs.data.close_price(s, at) for s in state["positions"]}
        equity = engine.book.equity(marks)
        demo, _, _ = decide(config, inputs, at, equity, state)
        engine._decide_and_fill(strategy, at)
        expected, _ = apply_limits(
            strategy.last_requested, strategy.last_context.equity_usd, config.limits
        )
        assert demo == expected
        assert demo[SYMBOL].notional_usd > 0


def test_future_candles_cannot_change_demo_decision(tmp_path):
    settings, _, _, journal, _, executor = setup_demo(tmp_path)
    original = market(executor)
    day = START + 400 * DAY
    config, _, _ = risk_config(settings.demo_risk_file)
    rows = [
        replace(row, close=999999, high=999999, open=999999) if row.open_time_ms >= day else row
        for values in original.data.rows.values()
        for row in values
    ]
    changed = replace(original, data=store(rows))
    assert decide(config, original, day, 1000, journal.state) == decide(
        config, changed, day, 1000, journal.state
    )


def test_halt_blocks_cycle_before_any_private_mutation(tmp_path):
    settings, clock, api, journal, client, executor = setup_demo(tmp_path)
    journal.halt("manual investigation pending")
    with pytest.raises(Halted, match="investigation"):
        run_cycle(settings, client, journal, executor.notifier, market(executor), clock)
    assert not mutations(api)


@pytest.mark.parametrize("partial", [False, True])
def test_interrupted_same_day_uses_checkpoint_not_second_signal(tmp_path, monkeypatch, partial):
    settings, clock, api, journal, client, executor = setup_demo(tmp_path)
    inputs = market(executor)
    day = START + 400 * DAY
    clock.set(day + 300000)
    api.last_price = str(inputs.data.close_price(SYMBOL, day))
    if partial:
        api.fill_fraction = Decimal("0.5")
    original_rebalance = type(executor).rebalance

    def fail_after_fill(self, *args, **kwargs):
        original_rebalance(self, *args, **kwargs)
        raise RuntimeError("power loss after fill before symbol checkpoint")

    monkeypatch.setattr(type(executor), "rebalance", fail_after_fill)
    with pytest.raises(RuntimeError):
        run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    assert journal.state["pending"] and not journal.state["pending_order"]
    created = len(mutations(api, "/v5/order/create"))
    monkeypatch.setattr(type(executor), "rebalance", original_rebalance)
    monkeypatch.setattr(
        "trending_basket.execution.cycle.decide",
        lambda *args: pytest.fail("recomputed a persisted decision"),
    )
    run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    assert len(mutations(api, "/v5/order/create")) == created
    assert journal.state["last_decision_ms"] == day


def test_cli_dry_run_is_read_only_and_does_not_create_checkpoint(tmp_path, monkeypatch):
    settings, clock, api, journal, client, executor = setup_demo(tmp_path)
    inputs = market(executor)
    day = START + 400 * DAY
    clock.set(day + 300000)
    api.last_price = str(inputs.data.close_price(SYMBOL, day))
    monkeypatch.setattr("trending_basket.execution.cli.demo_settings", lambda: settings)
    monkeypatch.setattr("trending_basket.execution.cli.SystemClock", lambda: clock)
    monkeypatch.setattr("trending_basket.execution.cli.BybitPrivateClient", lambda *a, **kw: client)
    monkeypatch.setattr("trending_basket.execution.cli.update_data", lambda *a: inputs)
    result = CliRunner().invoke(app, ["run", "--mode", "demo", "--once", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert '"status": "dry_run"' in result.output
    assert not mutations(api)
    assert not journal.path.exists()


def test_cli_rejects_live_before_data_or_private_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trending_basket.execution.cli.demo_settings", lambda: pytest.fail("loaded settings")
    )
    result = CliRunner().invoke(app, ["run", "--mode", "live", "--once"])
    assert result.exit_code != 0
    assert "--mode demo" in result.output
