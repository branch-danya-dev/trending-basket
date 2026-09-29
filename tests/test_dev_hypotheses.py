"""One-change hypotheses, closed BTC data and causal concentration ranking."""

import pytest
from backtest_support import DAY, FINE, START, ZERO, candle, config, store, universe

from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.sim_executor import SimExecutor
from trending_basket.strategies.base import DecisionContext, PositionView, TargetPosition
from trending_basket.strategies.trend_basket import TrendBasket, TrendBasketParams


def ctx(rows, symbols, at=START, positions=None):
    return DecisionContext(at, 1000, positions or {}, tuple(symbols), store(rows).view(at, symbols))


def trend_rows(symbol, slope, days=121):
    rows = []
    for day in range(-days, 0):
        value = 100 + (day + days) * slope
        rows.append(candle(day, open=value, high=value + 0.01, low=value - 0.01, symbol=symbol))
    return rows


def test_h1_changes_exit_channel_only():
    rows = [
        candle(-5, high=105, low=95),
        candle(-4, high=105, low=70),
        candle(-3, high=105, low=95),
        candle(-2, high=105, low=100),
        candle(-1, open=105, close=120),
    ]
    params = TrendBasketParams(lookbacks_days=(4,), atr_days=2)
    base, h1 = TrendBasket(params), TrendBasket(params.model_copy(update={"exit_ratio": 1.0}))
    first = ctx(rows, ["BTCUSDT"])
    assert base.decide(first) == h1.decide(first)
    rows.append(candle(0, open=120, close=96))
    second = ctx(rows, ["BTCUSDT"], START + DAY)
    assert base.decide(second)["BTCUSDT"].notional_usd == 0
    assert h1.decide(second)["BTCUSDT"].notional_usd > 0
    assert base.states["BTCUSDT"].atr == h1.states["BTCUSDT"].atr


@pytest.mark.parametrize(
    "alt_slope,btc_slope,allowed",
    [(1, 1, True), (1, -0.3, False), (-0.3, -0.3, True), (-0.3, 1, False)],
)
def test_h2_symmetric_sma_filter_and_btc_exemption(alt_slope, btc_slope, allowed):
    rows = trend_rows("ALT", alt_slope) + trend_rows("BTCUSDT", btc_slope)
    strategy = TrendBasket(TrendBasketParams(direction="long_short", btc_regime_filter="sma"))
    decision = strategy.decide(ctx(rows, ["ALT", "BTCUSDT"]))
    assert (decision["ALT"].notional_usd != 0) == allowed
    assert decision["BTCUSDT"].notional_usd * btc_slope > 0


@pytest.mark.parametrize("kind", ["missing", "short", "stale", "gap", "equal"])
def test_h2_missing_or_incomplete_btc_blocks_and_closes_alts(kind):
    btc = trend_rows("BTCUSDT", 1)
    if kind == "missing":
        btc = []
    if kind == "short":
        btc = btc[-99:]
    if kind == "stale":
        btc = btc[:-1]
    if kind == "gap":
        btc = [b for i, b in enumerate(btc) if i != 40]
    if kind == "equal":
        btc = [candle(i, symbol="BTCUSDT") for i in range(-121, 0)]
    rows = trend_rows("ALT", 1) + btc
    strategy = TrendBasket(TrendBasketParams(btc_regime_filter="sma"))
    decision = strategy.decide(
        ctx(rows, ["ALT", "BTCUSDT"], positions={"ALT": PositionView(1, 100, 90, 5, 220)})
    )
    assert decision["ALT"].notional_usd == 0


def test_h2_only_closed_btc_and_engine_supplies_btc_outside_universe():
    rows = trend_rows("ALT", 1) + trend_rows("BTCUSDT", -0.3)
    future = candle(0, open=10000, symbol="BTCUSDT")
    params = TrendBasketParams(btc_regime_filter="sma")
    assert TrendBasket(params).decide(ctx(rows, ["ALT", "BTCUSDT"])) == TrendBasket(params).decide(
        ctx([*rows, future], ["ALT", "BTCUSDT"])
    )
    conf = config(1)
    conf = conf.model_copy(update={"strategy_params": params.model_dump()})
    market = store([*rows, candle(0, open=221, symbol="ALT"), future])
    engine = BacktestEngine(conf, market, universe(["ALT"]), {"ALT": FINE}, {})
    result = engine.run(TrendBasket(params))
    assert not result.fills  # missing-universe BTC must still block the alt long


def test_h3_top_five_causal_ranking_and_existing_position_survives():
    symbols = ["A", "B", "C", "D", "E", "F", "G"]
    rows = [bar for i, s in enumerate(symbols, 1) for bar in trend_rows(s, i / 10)]
    params = TrendBasketParams(concentration_top_k=5, risk_per_symbol_frac=0.015)
    # A has the weakest score, but its existing position must not be liquidated.
    existing = {"A": PositionView(0.01, 100, 80, 1, 1.12)}
    strategy = TrendBasket(params)
    decision = strategy.decide(ctx(rows, symbols, positions=existing))
    new = {s for s, t in decision.items() if s not in existing and t.notional_usd}
    assert new == {"C", "D", "E", "F", "G"}
    assert 0 < decision["A"].notional_usd <= existing["A"].notional_usd
    assert decision["A"].allow_increase is False
    assert decision["B"].notional_usd == 0
    assert len(new) == 5
    future = [candle(0, open=10000, symbol=s) for s in symbols]
    assert decision == TrendBasket(params).decide(
        ctx([*rows, *future], symbols, positions=existing)
    )


def test_h3_equal_scores_use_symbol_order_and_insufficient_history_blocks():
    rows = [bar for s in ["Z", "A", "B"] for bar in trend_rows(s, 0.2)]
    params = TrendBasketParams(concentration_top_k=1)
    decision = TrendBasket(params).decide(ctx(rows, ["Z", "A", "B"]))
    assert {s for s, t in decision.items() if t.notional_usd} == {"A"}
    short_params = params.model_copy(update={"lookbacks_days": (2,), "atr_days": 2})
    short = trend_rows("A", 0.2, days=100)
    assert TrendBasket(short_params).decide(ctx(short, ["A"]))["A"].notional_usd == 0


def test_h3_no_actual_increase_on_gap_for_excluded_position():
    class Restricted:
        name = "test_concentration_gap"

        def decide(self, ctx):
            return {
                "BTCUSDT": TargetPosition(
                    200 if ctx.time_ms == START else 150, allow_increase=ctx.time_ms == START
                )
            }

    rows = [candle(-1), candle(0), candle(1, open=50)]
    result = BacktestEngine(config(2), store(rows), universe(), {"BTCUSDT": FINE}, {}).run(
        Restricted()
    )
    assert result.positions[0]["remaining_quantity"] <= 2
    assert not any(f["signed_quantity"] > 0 and f["time_ms"] > START for f in result.fills)


def test_exact_reduction_below_minimum_keeps_fractional_remainder():
    book = SimExecutor(1000, {"BTCUSDT": FINE}, ZERO, "exact")
    book.rebalance("BTCUSDT", TargetPosition(1), 100, 100, START)
    book.reduce_to("BTCUSDT", 0.009999999999, 100, START + 1, "limit")
    assert book.positions["BTCUSDT"].quantity == pytest.approx(0.009999999999, abs=1e-15)
    book.snapshot(START + 1, {"BTCUSDT": 100})


@pytest.mark.parametrize(
    "change",
    [
        {"btc_regime_filter": "ema"},
        {"concentration_top_k": 0},
        {"concentration_rank_days": 0},
        {"btc_regime_sma_days": 0},
    ],
)
def test_hypothesis_parameters_validate(change):
    with pytest.raises(ValueError):
        TrendBasketParams.model_validate(change)
