"""Independent arithmetic, event chronology and causality regressions."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest
from backtest_support import DAY, FINE, H4, START, ZERO, EnterOnce, candle, config, store, universe

from trending_basket.backtest.config import Costs
from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.backtest.sim_executor import InstrumentRules, SimExecutor
from trending_basket.domain.types import Interval
from trending_basket.portfolio.limits import PortfolioLimits, apply_limits
from trending_basket.strategies.base import TargetPosition
from trending_basket.strategies.benchmarks import BuyAndHoldBTC, EqualWeightUniverse, hold_positions


def test_four_bar_hand_calculation():
    # USD 1000. Bar 1: flat. Enter bar 2 at reference 100, buy slippage 1%:
    # execution 101, target 404 -> floor(404 / 101) = 4 units; fee 4*101*.001=.404.
    # Bar 2 close 110: cash 595.596 + inventory 440 = 1035.596.
    # Bar 3 funding: 4h open 120, positive rate .01 -> -4*120*.01 = -4.8.
    # Bar 3 close 105: equity 595.596-4.8+420 = 1010.796.
    # Bar 4 open 105, low 89 touches stop 90 (no gap); sell slip 2% -> 88.2.
    # Exit fee 4*88.2*.001=.3528. Cash 595.596-4.8+352.8-.3528=943.2432.
    # Independent gross-price identity: 1000 + 4*(90-100) - (.404+.3528)
    # - [4*(101-100)+4*(90-88.2)] - 4.8 = 943.2432. Net=-56.7568, R=-1.41892.
    rows = [
        candle(0),
        candle(1, close=110),
        candle(2, open=110, close=105),
        candle(3, open=105, low=89, close=100),
    ]
    fund_at = START + 2 * DAY + 2 * H4
    h4 = replace(candle(2, open=120, interval=Interval.H4), open_time_ms=fund_at)
    costs = Costs(taker_fee_bps=10, slippage_bps=100, stop_slippage_bps=200)
    rules = InstrumentRules(Decimal("1"), Decimal("1"), Decimal("1"), 2 * H4)
    result = BacktestEngine(
        config(costs=costs),
        store(rows, [h4]),
        universe(),
        {"BTCUSDT": rules},
        {"BTCUSDT": {fund_at: 0.01}},
    ).run(EnterOnce())
    assert [r["equity_usd"] for r in result.equity] == pytest.approx(
        [1000, 1000, 1035.596, 1010.796, 943.2432], abs=1e-9, rel=0
    )
    assert [f["time_ms"] for f in result.fills] == [START + DAY, START + 4 * DAY]
    assert result.equity[-1]["fees_usd"] == pytest.approx(0.7568, abs=1e-9, rel=0)
    assert result.equity[-1]["slippage_usd"] == pytest.approx(11.2, abs=1e-9, rel=0)
    assert result.equity[-1]["funding_usd"] == pytest.approx(-4.8, abs=1e-9, rel=0)
    assert result.positions[0]["return_r"] == pytest.approx(-1.41892, abs=1e-9, rel=0)
    metrics = calculate_metrics(result)
    # One observed payment cannot establish an interval: coverage must be unknown.
    assert metrics["funding_coverage_frac"] is None
    assert metrics["warnings"]
    assert sum(s["net_pnl_usd"] for s in metrics["by_symbol"].values()) == pytest.approx(-56.7568)


@pytest.mark.parametrize(
    "amount,stop,opening,high,low,reference",
    [
        (400, 90, 100, 110, 90, 90),
        (400, 90, 80, 100, 75, 80),
        (-400, 110, 100, 110, 90, 110),
        (-400, 110, 120, 130, 90, 120),
    ],
)
def test_long_short_stop_touch_and_gap(amount, stop, opening, high, low, reference):
    costs = ZERO.model_copy(
        update={"stop_slippage_bps": 50, "taker_fee_bps": 10, "rebalance_fill": "maker"}
    )
    rows = [candle(-1), candle(0, open=opening, high=high, low=low)]
    result = BacktestEngine(
        config(1, costs=costs), store(rows), universe(), {"BTCUSDT": FINE}, {}
    ).run(EnterOnce(amount, stop))
    exit = result.fills[-1]
    assert exit["reason"] == "stop"
    assert exit["reference_price"] == reference
    assert exit["price"] == pytest.approx(reference * (0.995 if amount > 0 else 1.005))
    assert exit["fee_usd"] == pytest.approx(exit["quantity"] * exit["price"] * 0.001)
    assert result.fills[0]["fee_usd"] == 0


@pytest.mark.parametrize("amount,expected", [(400, -4.8), (-400, 4.8)])
@pytest.mark.parametrize(
    "four_hour,price,source", [(True, 120, "4h_open"), (False, 100, "1d_open_fallback")]
)
def test_funding_sign_price_and_coverage(amount, expected, four_hour, price, source):
    at = START + 2 * H4
    h4 = replace(candle(0, open=120, interval=Interval.H4), open_time_ms=at)
    rows = [candle(-1), candle(0)]
    result = BacktestEngine(
        config(1),
        store(rows, [h4] if four_hour else []),
        universe(),
        {"BTCUSDT": FINE},
        {"BTCUSDT": {at: 0.01}},
    ).run(EnterOnce(amount, None))
    event = next(e for e in result.events if e["kind"] == "funding" and e["observed"])
    assert event["price_source"] == source
    assert event["payment_usd"] == pytest.approx(expected * price / 120)
    assert calculate_metrics(result)["funding_coverage_frac"] is None


def test_market_view_owns_only_closed_immutable_prefixes():
    data = store([candle(d) for d in range(5)], [candle(0, interval=Interval.H4)])
    view = data.view(START + DAY, ["BTCUSDT"])
    assert [c.open_time_ms for c in view.candles("BTCUSDT", Interval.D1, 100)] == [START]
    assert len(view.candles("BTCUSDT", Interval.H4, 100)) == 1
    assert view.candles("UNKNOWN", Interval.D1, 100) == ()
    with pytest.raises(FrozenInstanceError):
        view.candles("BTCUSDT", Interval.D1, 1)[0].close = 999
    with pytest.raises(ValueError):
        view.candles("BTCUSDT", Interval.D1, 0)
    assert not hasattr(view, "data")


def test_mutating_all_future_prices_and_funding_does_not_change_prefix():
    class HistoricalSignal:
        name = "causality"

        def decide(self, ctx):
            bars = ctx.market.candles("BTCUSDT", Interval.D1, 100)
            sign = 1 if bars[-1].close >= 100 else -1
            return {"BTCUSDT": TargetPosition(sign * ctx.equity_usd * 0.5, 80 if sign > 0 else 140)}

    cutoff = START + 3 * DAY
    daily = [candle(d, open=100 + d, high=130, low=90, close=110 - d) for d in range(-1, 8)]
    four_hour = [
        replace(candle(0, interval=Interval.H4), open_time_ms=START + i * H4) for i in range(8 * 6)
    ]
    rates = {START + i * 2 * H4: 0.001 for i in range(8 * 3)}

    def execute(rows, subrows, funding):
        return BacktestEngine(
            config(8), store(rows, subrows), universe(), {"BTCUSDT": FINE}, {"BTCUSDT": funding}
        ).run(HistoricalSignal())

    def change(c):
        return replace(c, open=500, high=999, low=1, close=2, volume=99999, turnover=7)

    original = execute(daily, four_hour, rates)
    changed = execute(
        [change(c) if c.open_time_ms >= cutoff else c for c in daily],
        [change(c) if c.open_time_ms >= cutoff else c for c in four_hour],
        {t: (-0.02 if t >= cutoff else r) for t, r in rates.items()},
    )
    assert [f for f in original.fills if f["time_ms"] < cutoff] == [
        f for f in changed.fills if f["time_ms"] < cutoff
    ]
    assert [r for r in original.equity if r["time_ms"] <= cutoff] == [
        r for r in changed.equity if r["time_ms"] <= cutoff
    ]
    assert original.equity[-1] != changed.equity[-1]  # Mutation really reaches the engine.


def test_intraday_delisting_uses_last_closed_4h_not_future_daily_close():
    at = START + H4 + 60_000
    rows = [candle(-1), candle(0, high=999, close=999), candle(1)]
    h4 = candle(0, close=80, interval=Interval.H4)
    costs = ZERO.model_copy(
        update={"delist_slippage_bps": 200, "taker_fee_bps": 10, "rebalance_fill": "maker"}
    )

    class Persistent:
        name = "persistent"

        def decide(self, ctx):
            return {"BTCUSDT": TargetPosition(400)}

    result = BacktestEngine(
        config(2, costs=costs),
        store(rows, [h4]),
        universe(until={"BTCUSDT": at}),
        {"BTCUSDT": FINE},
        {"BTCUSDT": {at: 0.9}},
    ).run(Persistent())
    assert len(result.fills) == 2
    exit = result.fills[-1]
    assert exit["time_ms"] == at
    assert exit["reason"] == "delisting"
    assert exit["reference_price"] == 80
    assert exit["price"] == pytest.approx(78.4)
    assert exit["fee_usd"] == pytest.approx(4 * 78.4 * 0.001)
    assert any(e["kind"] == "not_tradeable" for e in result.events)
    assert not any(e["kind"] == "funding" and e["time_ms"] >= at for e in result.events)


@pytest.mark.parametrize("exit_on_removal", [False, True])
def test_universe_removal_modes_and_blocked_increase(exit_on_removal):
    class Maintain:
        name = "maintain"

        def decide(self, ctx):
            targets = hold_positions(ctx)
            targets["BTCUSDT"] = TargetPosition(800 if ctx.positions else 400)
            targets["ETHUSDT"] = TargetPosition(100)  # Never selected.
            return targets

    univ = universe(("BTCUSDT", "ETHUSDT"), {START: ["BTCUSDT"], START + DAY: []})
    result = BacktestEngine(
        config(2, exit_on_universe_removal=exit_on_removal),
        store([candle(d) for d in range(-1, 2)]),
        univ,
        {"BTCUSDT": FINE, "ETHUSDT": FINE},
        {},
    ).run(Maintain())
    assert len(result.fills) == (2 if exit_on_removal else 1)
    assert all(f["symbol"] == "BTCUSDT" for f in result.fills)
    assert result.positions[0]["exit_reason"] == ("universe_removal" if exit_on_removal else "open")


@pytest.mark.parametrize(
    "field,targets,cap,expected",
    [
        ("max_gross_exposure", [2000, -1000], 1.5, [1000, -500]),
        ("max_net_exposure", [2000, 1000], 0.75, [500, 250]),
        ("max_symbol_exposure", [2000, -1000], 0.5, [500, -250]),
    ],
)
def test_each_limit_proportionally_scales_all_targets(field, targets, cap, expected):
    values = dict(max_gross_exposure=10, max_net_exposure=10, max_symbol_exposure=10)
    values[field] = cap
    decision = {s: TargetPosition(n, 100, 10) for s, n in zip(("A", "B"), targets, strict=True)}
    actual, triggered = apply_limits(decision, 1000, PortfolioLimits(**values))
    assert [p.notional_usd for p in actual.values()] == expected
    assert len(triggered) == 1
    assert actual["A"].stop_price == 100


def test_rounding_minimum_skips_and_reduce_only_exit():
    rule = InstrumentRules(Decimal(".1"), Decimal(".2"), Decimal("25"), 2 * H4)
    book = SimExecutor(1000, {"X": rule}, ZERO)
    for target in (1, -19, 24):
        book.rebalance("X", TargetPosition(target), 100, 100, 0)
    assert len(book.events) == 3 and not book.fills
    book.rebalance("X", TargetPosition(-39), 100, 100, 1)
    assert book.positions["X"].quantity == -0.3
    # Price collapse makes closing notional smaller than the entry minimum.
    book.close("X", 1, 2, "signal")
    assert not book.positions
    assert book.snapshot(2, {})["equity_usd"] == pytest.approx(1029.7)


def test_partial_reduction_increase_and_reversal_independent_ledger():
    book = SimExecutor(1000, {"X": FINE}, ZERO)
    book.rebalance("X", TargetPosition(400, initial_risk_usd=40), 100, 100, 0)  # +4
    book.rebalance("X", TargetPosition(220), 110, 110, 1)  # -2, realizes 20
    assert book.snapshot(1, {"X": 110})["equity_usd"] == 1040
    book.rebalance("X", TargetPosition(600), 120, 120, 2)  # +3, average 112
    book.rebalance("X", TargetPosition(-260), 130, 130, 3)  # -5 then -2
    assert len(book.closed_positions) == 1
    assert book.closed_positions[0]["gross_pnl_usd"] == 110
    assert book.snapshot(3, {"X": 130})["equity_usd"] == 1110
    book.close("X", 100, 4, "signal")
    assert book.snapshot(4, {})["equity_usd"] == 1170
    assert len(book.closed_positions) == 2
    assert book.closed_positions[0]["return_r"] == 2.75


def test_buy_hold_exact_price_return_and_no_spurious_daily_rebalance():
    rows = [candle(-1), *[candle(d, open=100 + 10 * d, close=110 + 10 * d) for d in range(4)]]
    result = BacktestEngine(config(), store(rows), universe(), {"BTCUSDT": FINE}, {}).run(
        BuyAndHoldBTC()
    )
    assert len(result.fills) == 1
    assert result.equity[-1]["equity_usd"] == 1400
    assert result.btc_price_return == pytest.approx(0.4)


def test_equal_weight_rebalances_on_month_change_only():
    symbols = ("BTCUSDT", "ETHUSDT")
    rows = [
        candle(d, open=100 if s == "BTCUSDT" else 200, symbol=s)
        for s in symbols
        for d in range(29, 34)
    ]
    univ = universe(symbols, {START: list(symbols), START + 31 * DAY: ["ETHUSDT"]})
    result = BacktestEngine(
        config(3, start=START + 30 * DAY), store(rows), univ, {s: FINE for s in symbols}, {}
    ).run(EqualWeightUniverse())
    assert [f["time_ms"] for f in result.fills] == [START + 30 * DAY] * 2 + [START + 31 * DAY] * 2
    assert len(result.positions) == 2


def test_off_grid_funding_and_boundary_ownership():
    # New orders do not owe funding at entry; the carried position owes the next boundary.
    rates = {START: 0.9, START + H4 // 4: 0.01, START + DAY: 0.02}
    result = BacktestEngine(
        config(2),
        store([candle(d) for d in range(-1, 2)]),
        universe(),
        {"BTCUSDT": FINE},
        {"BTCUSDT": rates},
    ).run(EnterOnce(400, None))
    observed = [e for e in result.events if e["kind"] == "funding" and e["observed"]]
    assert [e["time_ms"] for e in observed] == [START + H4 // 4, START + DAY]
    assert result.equity[-1]["funding_usd"] == -12
    assert observed[0]["price_source"] == "1d_open_fallback"


def test_partial_reduction_below_minimum_is_expanded_and_logged():
    rule = InstrumentRules(Decimal(".1"), Decimal(".2"), Decimal("25"), 2 * H4)
    book = SimExecutor(1000, {"X": rule}, ZERO)
    book.rebalance("X", TargetPosition(100), 100, 100, 0)
    book.rebalance("X", TargetPosition(90), 100, 100, 1)
    assert book.positions["X"].quantity == 0.7
    assert book.events[-1]["kind"] == "reduction_minimum_adjustment"


def test_missing_execution_bar_fails_instead_of_silently_holding_stale_price():
    with pytest.raises(ValueError, match="missing execution bar"):
        BacktestEngine(
            config(2), store([candle(-1), candle(0)]), universe(), {"BTCUSDT": FINE}, {}
        ).run(BuyAndHoldBTC())


def test_four_hour_execution_works_without_daily_warmup():
    experiment = config(1)
    experiment = experiment.model_copy(
        update={"run": experiment.run.model_copy(update={"interval": Interval.H4})}
    )
    rows = [
        replace(candle(0, open=100, close=110, interval=Interval.H4), open_time_ms=START + i * H4)
        for i in range(6)
    ]
    result = BacktestEngine(experiment, store(rows), universe(), {"BTCUSDT": FINE}, {}).run(
        BuyAndHoldBTC()
    )
    assert len(result.equity) == 7
    assert result.equity[-1]["equity_usd"] == 1100
    assert len(result.fills) == 1


def test_exact_minimum_notional_uses_decimal_boundary():
    rule = InstrumentRules(Decimal(".1"), Decimal(".1"), Decimal(".07"), 2 * H4)
    book = SimExecutor(10, {"X": rule}, ZERO)
    # Binary float .1 * .7 is .06999999999999999; the exact .07 minimum is met.
    book.rebalance("X", TargetPosition(0.07), 0.7, 0.7, 0)
    assert book.positions["X"].quantity == 0.1
    assert not book.events
