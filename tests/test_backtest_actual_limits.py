"""Regressions for executed exposure, including coarse lots and shrinking equity."""

import random
from dataclasses import replace
from decimal import Decimal

import pytest
from backtest_support import DAY, FINE, START, ZERO, EnterOnce, candle, config, store, universe

from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.sim_executor import InstrumentRules, SimExecutor
from trending_basket.portfolio.limits import PortfolioLimits, assert_actual_limits
from trending_basket.strategies.base import TargetPosition
from trending_basket.strategies.benchmarks import BuyAndHoldBTC


@pytest.mark.parametrize("direction", [1, -1])
def test_equity_drop_forces_reduction_larger_than_requested_minimum(direction):
    # 1 unit at 100, capital 100. Funding debits 1; desired reduction is only .01 units.
    # qtyStep=.1, minOrderQty=.3: reduce .3, leaving .7 units, never leave the old 1.0.
    rules = InstrumentRules(Decimal(".1"), Decimal(".3"), Decimal("0"), 28800000)
    book = SimExecutor(100, {"X": rules}, ZERO)
    book.rebalance("X", TargetPosition(direction * 100), 100, 100, START)
    book.funding("X", START + 1, direction * 0.01, 100, "4h_open")
    limits = PortfolioLimits(max_symbol_exposure=1)
    book.enforce_limits({"X": 100}, START + 1, limits)
    assert book.positions["X"].quantity == direction * 0.7
    assert_actual_limits({"X": direction * 70}, 99, limits)
    assert any(e["kind"] == "actual_limit_reduction" for e in book.events)
    assert any(e["kind"] == "reduction_minimum_adjustment" for e in book.events)


@pytest.mark.parametrize("direction", [1, -1])
def test_target_quantity_is_rounded_toward_zero_before_delta(direction):
    rule = InstrumentRules(Decimal("1"), Decimal("1"), Decimal("0"), 28800000)
    book = SimExecutor(1000, {"X": rule}, ZERO)
    book.rebalance("X", TargetPosition(direction * 1000), 100, 100, 0)
    book.rebalance("X", TargetPosition(direction * 950), 100, 100, 1)
    assert book.positions["X"].quantity == direction * 9


def test_minimum_reduction_can_close_position_completely():
    rule = InstrumentRules(Decimal(".1"), Decimal(".5"), Decimal("0"), 28800000)
    book = SimExecutor(1000, {"X": rule}, ZERO)
    book.rebalance("X", TargetPosition(50), 100, 100, 0)
    book.rebalance("X", TargetPosition(49), 100, 100, 1)
    assert not book.positions
    assert book.fills[-1]["quantity"] == 0.5
    assert book.events[-1]["remaining_quantity"] == 0


def test_actual_net_limit_survives_hedge_rounding_to_zero():
    # A skipped short entry leaves a net exposure despite nominal targets cancelling.
    rules = {"A": FINE, "B": replace(FINE, qty_step=Decimal("100"))}
    book = SimExecutor(1000, rules, ZERO)
    book.rebalance("A", TargetPosition(1000), 100, 100, 0)
    book.rebalance("B", TargetPosition(-1000), 100, 100, 0)
    limits = PortfolioLimits(max_symbol_exposure=2, max_net_exposure=0.1)
    book.enforce_limits({"A": 100, "B": 100}, 0, limits)
    assert book.positions["A"].quantity == pytest.approx(1)
    assert any(e.get("limit") == "net_exposure" for e in book.events)


def test_exposure_repair_includes_fees_and_slippage():
    costs = ZERO.model_copy(update={"taker_fee_bps": 100, "slippage_bps": 100})
    book = SimExecutor(1000, {"X": FINE}, costs)
    book.rebalance("X", TargetPosition(1000), 100, 100, 0)
    before = book.positions["X"].quantity
    book.enforce_limits({"X": 100}, 0, PortfolioLimits(max_symbol_exposure=1))
    assert book.positions["X"].quantity < before
    assert book.events[-1]["symbol_exposure"] <= 1 + 1e-9
    assert book.snapshot(0, {"X": 100})["equity_usd"] < 1000


def test_engine_checks_exposure_after_every_rebalance_on_equity_collapse():
    rows = [candle(-1), candle(0), candle(1), candle(2)]
    rule = InstrumentRules(Decimal(".1"), Decimal("3"), Decimal("0"), 28800000)
    result = BacktestEngine(
        config(3), store(rows), universe(), {"BTCUSDT": rule}, {"BTCUSDT": {START + DAY: 0.01}}
    ).run(BuyAndHoldBTC())
    checks = [e for e in result.events if e["kind"] == "post_rebalance"]
    assert len(checks) == 3
    for event in checks:
        assert event["gross_exposure"] <= 2 + 1e-9
        assert event["net_exposure"] <= 1.5 + 1e-9
        assert event["symbol_exposure"] <= 1 + 1e-9
    assert result.positions[0]["remaining_quantity"] == 7
    assert any(e["kind"] == "reduction_minimum_adjustment" for e in result.events)


def test_gap_open_revalues_positions_before_actual_limit_check():
    rows = [candle(-1), candle(0), candle(1, open=10)]
    result = BacktestEngine(config(2), store(rows), universe(), {"BTCUSDT": FINE}, {}).run(
        EnterOnce(1000, None)
    )
    assert all(
        e["symbol_exposure"] <= 1 + 1e-9 for e in result.events if e["kind"] == "post_rebalance"
    )


def test_invariant_rejects_actual_violation():
    with pytest.raises(ArithmeticError, match="exposure invariant"):
        assert_actual_limits({"X": 1001}, 1000, PortfolioLimits(max_symbol_exposure=1))


def test_outside_universe_cannot_increase_quantity_after_price_gap():
    book = SimExecutor(1000, {"X": FINE}, ZERO)
    book.rebalance("X", TargetPosition(500), 100, 100, 0)
    book.rebalance("X", TargetPosition(400), 100, 10, 1, allow_increase=False)
    assert book.positions["X"].quantity <= 5


def test_mixed_portfolios_always_finish_within_all_limits():
    rng = random.Random(731)
    for _ in range(80):
        rules = {
            s: replace(
                FINE,
                qty_step=Decimal(str(rng.choice([0.01, 0.1, 1]))),
                min_order_qty=Decimal(str(rng.choice([0.1, 1, 5]))),
            )
            for s in ("A", "B", "C")
        }
        costs = ZERO.model_copy(update={"taker_fee_bps": 10, "slippage_bps": 20})
        book = SimExecutor(1000, rules, costs)
        marks = {s: rng.uniform(10, 200) for s in rules}
        for s in rules:
            book.rebalance(s, TargetPosition(rng.uniform(-1800, 1800)), marks[s], marks[s], 0)
        limits = PortfolioLimits(
            max_gross_exposure=rng.uniform(0.3, 2),
            max_net_exposure=rng.uniform(0.05, 1),
            max_symbol_exposure=rng.uniform(0.1, 0.8),
        )
        previous = {s: abs(p.quantity) for s, p in book.positions.items()}
        book.enforce_limits(marks, 0, limits)
        assert_actual_limits(
            {s: p.quantity * marks[s] for s, p in book.positions.items()},
            book.equity(marks),
            limits,
        )
        assert all(abs(p.quantity) <= previous[s] for s, p in book.positions.items())
        book.snapshot(0, marks)
