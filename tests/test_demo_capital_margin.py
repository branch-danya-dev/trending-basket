"""Allocated capital, exchange funding, stress bounds and protective reentry."""

import copy
from decimal import Decimal as D

import pytest
from backtest_support import DAY, START
from demo_support import INSTRUMENT, SYMBOL, mutations, own_position, setup_demo
from test_demo_cycle import market

from trending_basket.execution import capital
from trending_basket.execution.cycle import check_account, drawdown_guard, run_cycle
from trending_basket.execution.journal import Halted, Journal
from trending_basket.execution.margin import cross_margin_stress, liquidation_distance
from trending_basket.execution.messages import reason_ru
from trending_basket.execution.planning import ExchangeRules
from trending_basket.strategies.base import TargetPosition


@pytest.mark.parametrize(
    "side,entry,bound,expected",
    [
        ("Buy", "100", "0.1", "99.9"),
        ("Sell", "100", "200", "100"),
    ],
)
def test_blank_liquidation_uses_directional_instrument_bound(side, entry, bound, expected):
    price = {"tickSize": ".1", "minPrice": bound, "maxPrice": bound}
    rules = ExchangeRules.from_api(INSTRUMENT | {"priceFilter": price})
    assert liquidation_distance(dict(side=side, avgPrice=entry, liqPrice=""), rules) == D(expected)


@pytest.mark.parametrize("boundary", [None, "", "NaN", "0", "101"])
def test_unknown_or_wrong_side_boundary_is_not_invented(boundary):
    price = {"tickSize": ".1", "minPrice": boundary}
    rules = ExchangeRules.from_api(INSTRUMENT | {"priceFilter": price})
    with pytest.raises(ValueError):
        liquidation_distance(dict(side="Buy", avgPrice="100", liqPrice=""), rules)


def wallet(rate=".2", mm="20"):
    return dict(
        accountMMRate=rate,
        totalMaintenanceMargin=mm,
        totalMarginBalance="100",
        totalAvailableBalance="80",
        totalInitialMargin="20",
    )


def test_account_stress_sums_all_stop_losses_and_never_credits_lower_mm():
    p = dict(side="Buy", markPrice="100", stopLoss="80", size="1")
    assert cross_margin_stress(wallet(), {"A": p})["stressed_mm_rate"] < 0.30
    with pytest.raises(ValueError, match="stress MMR"):
        cross_margin_stress(wallet(), {"A": p, "B": p})
    with pytest.raises(ValueError, match="at or above"):
        cross_margin_stress(wallet(".30", "30"), {})
    with pytest.raises(ValueError, match="margin data"):
        cross_margin_stress(wallet() | {"accountMMRate": ""}, {})
    # Positive totalEquity cannot hide an impaired effective margin denominator.
    with pytest.raises(ValueError, match="stress MMR"):
        cross_margin_stress(wallet() | {"totalAvailableBalance": "0"}, {})


def test_blank_liquidation_survives_when_bound_and_cross_margin_pass(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    api.liquidation = ""
    own_position(journal, api)
    executor.ensure_liquidation_distance(SYMBOL, 100, START)
    assert api.positions and not mutations(api)
    assert '"instrument_bound"' in (journal.directory / "events.jsonl").read_text()


def test_unsafe_account_margin_closes_owned_position_without_strategy_stop(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api)
    journal.state["strategy_states"] = {SYMBOL: {"subsystems": [1, 1, 1]}}
    api.wallet_override = {"accountMMRate": ".31"}
    with pytest.raises(Halted, match="account MMR"):
        executor.ensure_account_margin(START)
    assert not api.positions and not journal.state.get("recent_exits")
    assert journal.state["strategy_states"][SYMBOL]["subsystems"] == [1, 1, 1]
    assert journal.state["protected_reentries"][SYMBOL]["quantity"] == "2"


def test_wallet_collateral_cannot_change_size_or_drawdown(tmp_path):
    settings, clock, api, journal, client, executor = setup_demo(tmp_path)
    inputs = market(executor)
    day = START + 400 * DAY
    clock.set(day + 300000)
    journal.state["capital"]["funding_checked_ms"] = clock.now_ms()
    api.last_price = str(inputs.data.close_price(SYMBOL, day))
    journal.state["peak_equity_usd"] = 1000
    plans = []
    for equity in ("1000000", "100", "2000000"):
        api.equity = equity
        result = run_cycle(
            settings, client, journal, executor.notifier, inputs, clock, dry_run=True
        )
        plans.append(result["plan"])
        assert result["equity_usd"] == 1000
        drawdown_guard(journal, check_account(client, executor)["equity_usd"], 0.20)
    assert plans[0] == plans[1] == plans[2]
    assert not journal.state["halt"] and not mutations(api)


@pytest.mark.parametrize("payment", [-2, 3])
def test_exchange_fills_and_delayed_funding_are_counted_once_after_restart(tmp_path, payment):
    _, clock, api, journal, client, executor = setup_demo(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, clock.now_ms())
    clock.advance(1000)
    funding_time = clock.now_ms()
    # Transaction publication is delayed until AFTER the position has closed.
    clock.advance(1000)
    api.last_price = "110"
    executor.rebalance(SYMBOL, TargetPosition(0), 110, 110, clock.now_ms())
    assert not api.positions
    assert len(journal.state["orders"]) == 2
    api.transactions = [
        dict(
            id="funding-1",
            symbol=SYMBOL,
            category="linear",
            currency="USDT",
            type="SETTLEMENT",
            transactionTime=str(funding_time),
            funding=str(payment),
            size="1.9",
        )
    ]
    capital.sync_funding(journal, client)
    capital.sync_funding(journal, client)
    executor._record_fills(api.fills)  # Duplicate executions returned by overlapping queries.
    again = Journal(journal.directory, clock)
    capital.sync_funding(again, client)
    result = capital.snapshot(again, {})
    assert result["realized_pnl_usd"] == pytest.approx(19)
    assert result["fees_usd"] == pytest.approx(1.9 * 210 * 0.00055)
    assert result["funding_usd"] == payment
    assert result["equity_usd"] == pytest.approx(1000 + 19 - 1.9 * 210 * 0.00055 + payment)
    assert len(again.state["capital"]["funding"]) == 1
    assert len((journal.directory / "funding.jsonl").read_text().splitlines()) == 1


def test_real_unrealized_pnl_changes_allocated_equity_and_can_halt(tmp_path):
    _, _, api, journal, client, executor = setup_demo(tmp_path)
    own_position(journal, api)
    api.positions[SYMBOL]["unrealisedPnl"] = "-201"
    api.equity = "9999999"
    result = check_account(client, executor)
    assert result["equity_usd"] == 799
    journal.state["peak_equity_usd"] = 1000
    with pytest.raises(Halted, match="drawdown"):
        drawdown_guard(journal, result["equity_usd"], 0.20)


@pytest.mark.parametrize("reason", ["liquidation_distance", "unprotected_exit", "emergency_exit"])
def test_protective_close_reenters_next_cycle_and_shadow_remains_in_position(tmp_path, reason):
    settings, clock, api, journal, client, executor = setup_demo(tmp_path)
    inputs = market(executor)
    day = START + 400 * DAY
    clock.set(day + 300000)
    journal.state["capital"]["funding_checked_ms"] = clock.now_ms()
    api.last_price = str(inputs.data.close_price(SYMBOL, day))
    run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    subsystem_state = copy.deepcopy(journal.state["strategy_states"])
    shadow_position = copy.deepcopy(journal.state["shadow_book"]["positions"][SYMBOL])
    quantity = D(api.positions[SYMBOL]["size"])
    executor._submit(SYMBOL, -quantity, float(api.last_price), day, TargetPosition(0), reason)
    assert not api.positions and not journal.state.get("recent_exits")
    assert journal.state["strategy_states"] == subsystem_state
    assert journal.state["shadow_book"]["positions"][SYMBOL] == shadow_position
    clock.set(day + DAY + 300000)
    api.last_price = str(inputs.data.close_price(SYMBOL, day + DAY))
    run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    assert api.positions[SYMBOL]["stopLoss"] != "0"
    assert (
        journal.state["shadow_book"]["positions"][SYMBOL]["lifecycle_id"]
        == shadow_position["lifecycle_id"]
    )
    assert (
        journal.state["strategy_states"][SYMBOL]["subsystems"]
        == subsystem_state[SYMBOL]["subsystems"]
    )


def test_legacy_migration_is_flat_only_preserves_records_and_runs_once(tmp_path):
    _, clock, api, journal, _, _ = setup_demo(tmp_path)
    journal.state.pop("capital")
    journal.state["orders"] = {"old-order": {"status": "Filled"}}
    journal.state["last_decision_ms"] = START
    journal.state["peak_equity_usd"] = 185000
    capital.initialize(journal, 1000, clock.now_ms())
    assert journal.state["last_decision_ms"] is None
    assert journal.state["peak_equity_usd"] == 1000 and "old-order" in journal.state["orders"]
    before = journal.path.read_bytes()
    capital.initialize(journal, 1000, clock.now_ms())
    assert journal.path.read_bytes() == before
    with pytest.raises(ValueError, match="changed"):
        capital.initialize(journal, 2000, clock.now_ms())
    journal.state.pop("capital")
    own_position(journal, api)
    with pytest.raises(ValueError, match="flat"):
        capital.initialize(journal, 1000, clock.now_ms())


def test_russian_notification_reason_does_not_expose_english_exception():
    for reason in ("position mismatch", "unknown internal exception", "liquidation bound missing"):
        result = reason_ru(reason)
        assert reason not in result
        assert any("\u0400" <= ch <= "\u04ff" for ch in result)


def test_missing_allocated_setting_is_rejected(tmp_path):
    _, clock, _, journal, _, _ = setup_demo(tmp_path)
    journal.state.pop("capital")
    with pytest.raises(ValueError, match="TB_ALLOCATED_CAPITAL_USD"):
        capital.initialize(journal, None, clock.now_ms())
