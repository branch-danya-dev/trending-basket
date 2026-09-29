"""Frozen T004 regression, fractional fills, risk repair and scale invariance."""

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from backtest_support import DAY, FINE, START, ZERO, candle, config, store, universe
from typer.testing import CliRunner

from trending_basket.backtest.config import Costs, Execution, load_experiment
from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.backtest.reporting import save_report
from trending_basket.backtest.sim_executor import InstrumentRules, SimExecutor
from trending_basket.cli import app
from trending_basket.clock import ManualClock
from trending_basket.portfolio.limits import PortfolioLimits, assert_actual_limits
from trending_basket.strategies.base import TargetPosition
from trending_basket.strategies.benchmarks import BuyAndHoldBTC
from trending_basket.strategies.trend_basket import TrendBasket


def frozen_market():
    rows = []
    for day in range(-110, 140):
        phase = (day + 110) % 80
        close = 100 + phase if phase < 40 else 180 - phase
        rows.append(candle(day, open=close - 0.5, high=close + 0.25, low=close - 0.75, close=close))
    funding = {START + i * DAY: (0.0001 if i % 3 else -0.0001) for i in range(140)}
    rules = {"BTCUSDT": InstrumentRules(Decimal(".1"), Decimal(".3"), Decimal("5"), DAY)}
    return rows, funding, rules


@pytest.mark.parametrize("name", ["v1", "btc"])
def test_exchange_matches_frozen_t004_output_byte_for_byte(tmp_path, name):
    rows, funding, rules = frozen_market()
    conf = config(140, costs=Costs())
    assert conf.execution.quantity_mode == "exchange"
    strategy = TrendBasket() if name == "v1" else BuyAndHoldBTC()
    result = BacktestEngine(conf, store(rows), universe(), rules, {"BTCUSDT": funding}).run(
        strategy
    )
    fixture = json.loads(
        (Path("tests/fixtures/backtest") / f"t004_{name}_exchange.json").read_text()
    )
    assert fixture["fills"]
    if name == "v1":
        assert len(fixture["positions"]) >= 2 and any(
            f["reason"] == "stop" for f in fixture["fills"]
        )
    for key, expected in fixture.items():
        actual = getattr(result, key)
        assert actual == expected
        a, b = tmp_path / f"{key}-actual.parquet", tmp_path / f"{key}-frozen.parquet"
        pd.DataFrame(actual).to_parquet(a, index=False)
        pd.DataFrame(expected, columns=pd.DataFrame(actual).columns).to_parquet(b, index=False)
        assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("mode", ["exact", "exchange"])
def test_quantity_and_minimum_semantics(sign, mode):
    rules = {"BTCUSDT": InstrumentRules(Decimal("1"), Decimal("2"), Decimal("500"), DAY)}
    book = SimExecutor(1000, rules, Costs(), mode)
    book.rebalance("BTCUSDT", TargetPosition(sign * 123), 100, 100, START)
    if mode == "exact":
        assert book.positions["BTCUSDT"].quantity == pytest.approx(
            sign * 123 / (100 * (1 + sign * 0.0002))
        )
        assert not book.events
        book.rebalance("BTCUSDT", TargetPosition(sign * 0.1), 100, 100, START + 1)
        assert abs(book.positions["BTCUSDT"].quantity) < 0.002
        book.funding("BTCUSDT", START + 2, 0.01, 100, "4h_open")
        book.close("BTCUSDT", 120, START + 3, "signal")
        book.snapshot(START + 3, {})  # independent cash/PnL identity after costs and funding
        assert not any(e["kind"].startswith("minimum") for e in book.events)
    else:
        assert not book.positions
        assert book.events[-1]["kind"] == "minimum_order_skip"


def test_exact_has_no_hidden_tiny_quantity_minimum():
    book = SimExecutor(1000, {"BTCUSDT": FINE}, ZERO, "exact")
    book.rebalance("BTCUSDT", TargetPosition(0.1), 1e12, 1e12, START)
    assert book.positions["BTCUSDT"].quantity == 0.1 / 1e12
    book.snapshot(START, {"BTCUSDT": 1e12})
    book.close("BTCUSDT", 2e12, START + 1, "signal")
    assert book.snapshot(START + 1, {})["equity_usd"] == pytest.approx(1000.1)


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("mode", ["exchange", "exact"])
def test_actual_risk_repair_after_funding_and_costs(mode, sign):
    book = SimExecutor(1000, {"BTCUSDT": FINE}, Costs(), mode)
    book.rebalance("BTCUSDT", TargetPosition(sign * 900), 100, 100, START)
    book.funding("BTCUSDT", START + 1, sign * 0.1, 100, "4h_open")
    limits = PortfolioLimits(max_gross_exposure=0.5, max_net_exposure=0.4, max_symbol_exposure=0.3)
    book.enforce_limits({"BTCUSDT": 100}, START + 2, limits)
    notionals = {s: p.quantity * 100 for s, p in book.positions.items()}
    assert_actual_limits(notionals, book.equity({"BTCUSDT": 100}), limits)
    assert any(e["kind"] == "actual_limit_reduction" for e in book.events)
    book.snapshot(START + 2, {"BTCUSDT": 100})
    if mode == "exact":
        assert not any(e["kind"] == "reduction_minimum_adjustment" for e in book.events)


def test_exact_scale_invariance_and_metadata(tmp_path):
    rows, funding, rules = frozen_market()
    results = []
    for capital in (1000, 10000):
        conf = config(140, costs=Costs())
        conf = conf.model_copy(
            update={
                "execution": Execution(quantity_mode="exact"),
                "run": conf.run.model_copy(update={"initial_capital_usd": capital}),
            }
        )
        results.append(
            BacktestEngine(conf, store(rows), universe(), rules, {"BTCUSDT": funding}).run(
                TrendBasket()
            )
        )
    for a, b in zip(results[0].equity, results[1].equity, strict=True):
        assert a["equity_usd"] == pytest.approx(b["equity_usd"] / 10, abs=1e-8)
    metrics = calculate_metrics(results[1])
    assert metrics["minimum_order_skips"] == 0
    assert metrics["sharpe_standard_error"] == pytest.approx(
        ((1 + metrics["sharpe"] ** 2 / 2) / (140 / 365)) ** 0.5
    )
    assert metrics["average_position_notional_usd"] > 0
    dest = save_report(tmp_path, conf, "", results[1], metrics, [], ManualClock(START))
    assert json.loads((dest / "manifest.json").read_text())["quantity_mode"] == "exact"
    assert json.loads((dest / "metrics.json").read_text())["quantity_mode"] == "exact"
    assert (dest / "report.md").read_text().startswith("# test [quantity_mode=exact]")
    compare = CliRunner().invoke(app, ["backtest", "compare", str(dest)])
    assert "| Run | Quantity mode |" in compare.output and "| exact |" in compare.output


def test_preregistered_config_modes_and_hypothesis_deltas():
    models = {
        n: load_experiment(Path(f"experiments/{n}.toml"))
        for n in ("V1x", "V2x", "V3x", "V4x", "H1", "H2", "H3", "H3-1k", "V2-1k")
    }
    for name, conf in models.items():
        assert conf.run.period == "dev"
        assert conf.execution.quantity_mode == ("exchange" if name.endswith("-1k") else "exact")
    assert models["H1"].strategy_params == {**models["V2x"].strategy_params, "exit_ratio": 1.0}
    with pytest.raises(ValueError):
        Execution(quantity_mode="approximate")
