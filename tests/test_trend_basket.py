"""Independent synthetic checks for the preregistered trend rules."""

from dataclasses import replace
from decimal import Decimal

import pytest
from backtest_support import DAY, FINE, H4, START, ZERO, candle, config, store, universe

from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.backtest.sim_executor import InstrumentRules, SimExecutor
from trending_basket.domain.types import Interval
from trending_basket.portfolio.limits import PortfolioLimits, apply_limits
from trending_basket.strategies.base import (
    DecisionContext,
    PositionExit,
    PositionView,
    TargetPosition,
)
from trending_basket.strategies.benchmarks import BuyAndHoldBTC
from trending_basket.strategies.trend_basket import TrendBasket, TrendBasketParams

SYMBOL = "BTCUSDT"


def context(rows, positions=None, exits=(), at=None, extra=(), members=(SYMBOL,)):
    at = at or rows[-1].open_time_ms + rows[-1].interval.duration_ms
    return DecisionContext(
        at, 1000, positions or {}, members, store(rows, extra).view(at, [SYMBOL]), exits
    )


def simple_params(**kwargs):
    return TrendBasketParams(lookbacks_days=(2, 3, 4), atr_days=2, **kwargs)


def test_subsystems_activate_one_by_one_and_exclude_current_high():
    # Older peaks 140,130,110 expire in order from the three channels.
    rows = [candle(0, high=140), candle(1, high=130), candle(2, high=110), candle(3, high=105)]
    strategy = TrendBasket(simple_params())
    for day, close, expected in [(4, 115, [1, 0, 0]), (5, 125, [1, 1, 0]), (6, 135, [1, 1, 1])]:
        # Current high=999 must not prevent the first breakout; later bars use that high.
        high = 116 if day == 4 else close
        rows.append(candle(day, close=close, high=high))
        decision = strategy.decide(context(rows))
        assert strategy.states[SYMBOL].subsystems == expected
        atr = strategy.states[SYMBOL].atr
        full = 0.005 * 1000 / (3 * atr / close)
        assert decision[SYMBOL].notional_usd == pytest.approx(sum(expected) / 3 * full)
    isolated = [*rows[:4], candle(4, close=115, high=999)]
    s = TrendBasket(simple_params())
    assert s.decide(context(isolated))[SYMBOL].notional_usd > 0
    assert s.states[SYMBOL].subsystems == [1, 0, 0]


def test_equality_is_not_breakout_and_falling_long_only_remains_flat():
    rows = [candle(i, open=110 - 2 * i, high=111 - 2 * i, low=109 - 2 * i) for i in range(12)]
    long_only = TrendBasket(simple_params())
    assert long_only.decide(context(rows))[SYMBOL].notional_usd == 0
    short = TrendBasket(simple_params(direction="long_short"))
    assert short.decide(context(rows))[SYMBOL].notional_usd < 0
    equal = [candle(i, high=110, low=90, close=110) for i in range(5)]
    assert TrendBasket(simple_params()).decide(context(equal))[SYMBOL].notional_usd == 0


def test_manual_atr_sizing_and_wilder_update():
    # Last 2 TRs: 4 and 6, ATR=5. Close=110, equity=1000, full f=1.
    # Target=.005*1000/(3*5/110)=110/3; risk=target*15/110=$5.
    rows = [
        candle(0, open=100),
        candle(1, open=102, high=104, low=100),
        candle(2, open=106, high=110, low=106, close=110),
    ]
    # TR2 is 8 relative to previous close 102; use previous close104 for TR=6.
    rows[1] = replace(rows[1], close=104)
    strategy = TrendBasket(TrendBasketParams(lookbacks_days=(1, 2), atr_days=2))
    target = strategy.decide(context(rows))[SYMBOL]
    assert strategy.states[SYMBOL].atr == 5
    assert target.notional_usd == pytest.approx(110 / 3)
    assert target.initial_risk_usd == pytest.approx(5)
    assert target.stop_price == 95
    rows.append(candle(3, open=111, high=112, low=110, close=111))
    strategy.decide(context(rows))
    assert strategy.states[SYMBOL].atr == 3.5  # (5+2)/2


@pytest.mark.parametrize("long", [True, False])
def test_stop_never_loosens_with_wider_atr_and_partial_position_changes(long):
    strategy = TrendBasket(simple_params(direction="long_short"))
    rows = [
        candle(
            i,
            open=100 + (i if long else -i),
            high=101 + (i if long else -i),
            low=99 + (i if long else -i),
        )
        for i in range(5)
    ]
    # Ensure strict breakout rather than equality to the preceding high/low.
    rows[-1] = candle(4, open=100, close=110 if long else 90)
    initial = strategy.decide(context(rows))[SYMBOL]
    previous = initial.stop_price
    assert previous is not None
    for i, spread in enumerate((1, 20, 2, 30), 5):
        close = 110 + i if long else 90 - i
        rows.append(candle(i, open=close, high=close + spread, low=close - spread))
        signed = 1 if long else -1
        position = PositionView(signed, 100, previous, 5, signed * close, START + 5 * DAY, 1)
        target = strategy.decide(context(rows, {SYMBOL: position}))[SYMBOL]
        assert target.stop_price >= previous if long else target.stop_price <= previous
        previous = target.stop_price


def test_stop_resets_every_subsystem_and_requires_strictly_later_breakout():
    rows = [candle(i, high=101, low=99) for i in range(4)] + [candle(4, close=110)]
    strategy = TrendBasket(simple_params())
    strategy.decide(context(rows))
    assert strategy.states[SYMBOL].subsystems == [1, 1, 1]
    rows.append(candle(5, open=120, high=125, low=115, close=125))
    stopped_at = START + 6 * DAY
    result = strategy.decide(context(rows, exits=(PositionExit(SYMBOL, stopped_at, "stop"),)))
    assert result[SYMBOL].notional_usd == 0
    assert strategy.states[SYMBOL].subsystems == [0, 0, 0]
    rows.append(candle(6, open=120, high=124, low=119, close=123))
    assert strategy.decide(context(rows))[SYMBOL].notional_usd == 0
    rows.append(candle(7, open=130, close=131))
    assert strategy.decide(context(rows))[SYMBOL].notional_usd > 0


def test_rebalance_band_holds_quantity_but_updates_stop():
    rows = [candle(i, high=101, low=99) for i in range(4)] + [candle(4, close=110)]
    strategy = TrendBasket(simple_params(rebalance_band_frac=0.25))
    target = strategy.decide(context(rows))[SYMBOL]
    qty = target.notional_usd / 110
    rows.append(candle(5, open=110, high=114, low=108, close=111))
    position = PositionView(
        qty, 110, target.stop_price, target.initial_risk_usd, qty * 111, START + 5 * DAY, 1
    )
    next_target = strategy.decide(context(rows, {SYMBOL: position}))[SYMBOL]
    assert next_target.notional_usd == position.notional_usd
    assert next_target.stop_price >= position.stop_price
    executor = SimExecutor(1000, {SYMBOL: FINE}, ZERO)
    executor.rebalance(SYMBOL, target, 110, 110, START)
    before = len(executor.fills)
    actual = executor.views({SYMBOL: 111})[SYMBOL]
    executor.rebalance(
        SYMBOL, replace(next_target, notional_usd=actual.notional_usd), 111, 111, START + DAY
    )
    assert len(executor.fills) == before


def test_four_hour_atr_uses_only_closed_daily_bar():
    daily = [candle(i, high=102, low=98) for i in range(4)]
    # Enormous current daily range is invisible at 04:00, visible at next midnight.
    daily.append(candle(4, high=900, low=1))
    h4 = [
        replace(candle(0, interval=Interval.H4, high=101, low=99), open_time_ms=START + i * H4)
        for i in range(25)
    ]
    h4[-1] = replace(h4[-1], high=110, close=110)
    at = START + 4 * DAY + H4
    strategy = TrendBasket(simple_params(), Interval.H4)
    target = strategy.decide(context(h4, at=at, extra=daily))[SYMBOL]
    assert strategy.states[SYMBOL].atr == 4
    assert target.initial_risk_usd == pytest.approx(5)
    assert target.stop_price == 98
    changed = [*daily[:-1], replace(daily[-1], high=10000)]
    assert (
        target
        == TrendBasket(simple_params(), Interval.H4).decide(context(h4, at=at, extra=changed))[
            SYMBOL
        ]
    )


def test_removed_symbol_cannot_increase_or_reverse():
    rows = [candle(i, high=101, low=99) for i in range(4)] + [candle(4, close=110)]
    position = PositionView(0.01, 100, 90, 1, 1.1)
    target = TrendBasket(simple_params()).decide(context(rows, {SYMBOL: position}, members=()))[
        SYMBOL
    ]
    assert 0 <= target.notional_usd <= 1.1


def test_actual_entry_risk_rounding_limits_and_lifecycle_max():
    book = SimExecutor(
        1000, {SYMBOL: InstrumentRules(Decimal("1"), Decimal("1"), None, 2 * H4)}, ZERO
    )
    target = TargetPosition(150, 70, 15)
    limited, _ = apply_limits({SYMBOL: target}, 100, PortfolioLimits(max_symbol_exposure=1))
    assert limited[SYMBOL].initial_risk_usd == 10
    book.rebalance(SYMBOL, target, 100, 100, START)
    assert book.positions[SYMBOL].initial_risk_usd == 10  # floor(150/100)=1
    book.rebalance(SYMBOL, TargetPosition(300, 75, 60), 100, 100, START + DAY)
    assert book.positions[SYMBOL].initial_risk_usd == 60
    book.rebalance(SYMBOL, TargetPosition(400, 75, 40), 100, 100, START + 2 * DAY)
    assert book.positions[SYMBOL].initial_risk_usd == 60
    book.close(SYMBOL, 90, START + 3 * DAY, "signal")
    assert book.closed_positions[0]["return_r"] == pytest.approx(-40 / 60)


def test_btc_pure_hold_survives_funding_without_trimming():
    rows = [candle(d) for d in range(-1, 4)]
    result = BacktestEngine(
        config(enabled=False),
        store(rows),
        universe(),
        {SYMBOL: FINE},
        {SYMBOL: {START + DAY: 0.1, START + 2 * DAY: 0.1}},
    ).run(BuyAndHoldBTC())
    assert len(result.fills) == 1
    assert result.positions[0]["remaining_quantity"] == 10
    assert result.equity[-1]["equity_usd"] == 800
    assert result.equity[-1]["gross_exposure"] == 1.25


def test_trend_future_mutation_and_repeatability(tmp_path):
    import pandas as pd

    rows = [candle(i, open=100 + i * 2, high=101 + i * 2, low=99 + i * 2) for i in range(-4, 14)]
    cutoff = START + 7 * DAY

    def execute(data):
        return BacktestEngine(config(14), store(data), universe(), {SYMBOL: FINE}, {}).run(
            TrendBasket(simple_params())
        )

    first, second = execute(rows), execute(rows)
    mutated = execute(
        [replace(c, high=999, low=1, close=3) if c.open_time_ms >= cutoff else c for c in rows]
    )
    assert first.fills and first.equity[-1] != mutated.equity[-1]
    assert [f for f in first.fills if f["time_ms"] < cutoff] == [
        f for f in mutated.fills if f["time_ms"] < cutoff
    ]
    assert [e for e in first.equity if e["time_ms"] <= cutoff] == [
        e for e in mutated.equity if e["time_ms"] <= cutoff
    ]
    for name in ("fills", "positions", "events", "equity"):
        a, b = tmp_path / f"a-{name}.parquet", tmp_path / f"b-{name}.parquet"
        pd.DataFrame(getattr(first, name)).to_parquet(a, index=False)
        pd.DataFrame(getattr(second, name)).to_parquet(b, index=False)
        assert a.read_bytes() == b.read_bytes()
    metrics = calculate_metrics(first)
    assert sum(v["fees_usd"] for v in metrics["by_direction"].values()) == metrics["fees_usd"]


@pytest.mark.parametrize(
    "values",
    [
        {"lookbacks_days": []},
        {"lookbacks_days": [0]},
        {"lookbacks_days": [20, 20]},
        {"atr_days": 0},
        {"exit_ratio": 2},
        {"direction": "both"},
    ],
)
def test_invalid_parameters(values):
    with pytest.raises(ValueError):
        TrendBasketParams.model_validate(values)


def test_default_twenty_fifty_five_hundred_day_subsystems():
    rows = [candle(i, high=101, low=99) for i in range(100)]
    rows[44] = replace(rows[44], high=130)
    rows[79] = replace(rows[79], high=120)
    rows[99] = replace(rows[99], high=110)
    strategy = TrendBasket()
    for i, close, count in [(100, 115, 1), (101, 125, 2), (102, 135, 3)]:
        rows.append(candle(i, close=close))
        strategy.decide(context(rows))
        assert sum(strategy.states[SYMBOL].subsystems) == count


def test_engine_delivers_stop_and_prevents_same_close_reentry():
    rows = [candle(i, high=101, low=99) for i in range(-5, -1)]
    rows += [
        candle(-1, close=110),
        candle(0, open=110, high=125, low=90, close=125),
        candle(1, open=125, close=130),
        candle(2, open=130, close=131),
    ]
    result = BacktestEngine(config(3), store(rows), universe(), {SYMBOL: FINE}, {}).run(
        TrendBasket(simple_params())
    )
    assert [(f["reason"], f["time_ms"]) for f in result.fills] == [
        ("signal", START),
        ("stop", START + DAY),
        ("signal", START + 2 * DAY),
    ]


def test_closed_lifecycle_side_costs_and_r_ranking():
    class Reverse:
        name = "test_reverse"

        def decide(self, ctx):
            target = 300 if ctx.time_ms == START else -300 if ctx.time_ms == START + DAY else 0
            return {SYMBOL: TargetPosition(target, initial_risk_usd=30 if target else None)}

    costs = ZERO.model_copy(update={"taker_fee_bps": 10, "slippage_bps": 10})
    rates = {START + H4: 0.01, START + DAY + H4: 0.01}
    result = BacktestEngine(
        config(3, costs=costs),
        store([candle(d) for d in range(-1, 3)]),
        universe(),
        {SYMBOL: FINE},
        {SYMBOL: rates},
    ).run(Reverse())
    metrics = calculate_metrics(result)
    assert metrics["by_direction"]["long"]["funding_paid_usd"] > 0
    assert metrics["by_direction"]["short"]["funding_received_usd"] > 0
    for key in ("fees_usd", "slippage_usd", "funding_usd"):
        assert sum(row[key] for row in metrics["by_direction"].values()) == pytest.approx(
            metrics[key]
        )
    assert metrics["r_distribution"]["best_five"][0]["direction"] == "short"
    assert metrics["r_distribution"]["worst_five"][0]["direction"] == "long"
