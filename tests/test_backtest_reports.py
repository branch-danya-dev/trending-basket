"""Offline CLI roundtrip, byte determinism, metrics and bounded engine benchmark."""

import json
import math
import time
from dataclasses import asdict, replace
from decimal import Decimal

import pandas as pd
import pytest
from backtest_support import DAY, FINE, START, candle, config, store, universe
from typer.testing import CliRunner

from trending_basket.backtest import cli, reporting
from trending_basket.backtest.config import load_experiment
from trending_basket.backtest.engine import BacktestEngine, BacktestResult
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.cli import app
from trending_basket.clock import ManualClock
from trending_basket.data.cache import funding_path, klines_path
from trending_basket.domain.types import Interval
from trending_basket.strategies.benchmarks import BuyAndHoldBTC, EqualWeightUniverse
from trending_basket.universe.storage import save_universe


def seed(tmp_path):
    snapshot = tmp_path / "data/bybit/linear/instruments/2024-01-05.parquet"
    snapshot.parent.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "BTCUSDT",
                "qty_step": "0.001",
                "min_order_qty": "0.001",
                "min_notional_value": "5",
                "funding_interval_ms": 28_800_000,
            }
        ]
    ).to_parquet(snapshot)
    save_universe(tmp_path / "data", "test", universe())
    for interval in Interval:
        path = klines_path(tmp_path / "data", interval, "BTCUSDT")
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([asdict(candle(d, interval=interval)) for d in range(-1, 5)]).to_parquet(path)
    path = funding_path(tmp_path / "data", "BTCUSDT")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "funding_time_ms": list(range(START, START + 4 * DAY, 28_800_000)),
            "rate_frac": [0.0001] * 12,
        }
    ).to_parquet(path)
    toml = tmp_path / "experiment.toml"
    toml.write_text(
        '[run]\nname="offline"\nstrategy="buy_and_hold_btc"\n'
        'universe="test"\nstart="2024-01-01"\nend="2024-01-04"\n'
        "initial_capital_usd=1000\n[limits]\nmax_symbol_exposure=1\n",
        encoding="utf-8",
    )
    return toml


def test_cli_roundtrip_identical_parquets_and_metrics(tmp_path, monkeypatch):
    toml = seed(tmp_path)
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TB_REPORTS_DIR", str(tmp_path / "reports"))
    clock = ManualClock(START)
    monkeypatch.setattr(cli, "SystemClock", lambda: clock)
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(app, ["backtest", "run", str(toml)])
        assert result.exit_code == 0, result.output
        clock.advance(1000)
    a, b = sorted((tmp_path / "reports").iterdir())
    for name in (
        "fills.parquet",
        "positions.parquet",
        "equity.parquet",
        "events.parquet",
        "metrics.json",
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    manifest = json.loads((a / "manifest.json").read_text())
    assert manifest["experiment_toml"] == toml.read_text()
    assert manifest["package_version"]
    assert manifest["git"]["commit"]
    assert len(manifest["inputs"]) == 6
    assert all(i["sha256"] for i in manifest["inputs"])
    for name in ("equity.png", "drawdown.png"):
        assert (a / name).read_bytes().startswith(b"\x89PNG")
    assert "funding_coverage_frac" in runner.invoke(app, ["backtest", "show", str(a)]).output
    compare = runner.invoke(app, ["backtest", "compare", str(a), str(b)])
    assert compare.exit_code == 0 and a.name in compare.output and b.name in compare.output
    for label in ("BTC price only, no funding or costs", "gross funding paid on BTC"):
        assert label in compare.output
        assert label in (a / "report.md").read_text()
    assert "Funding coverage by symbol" in (a / "report.md").read_text()
    assert "Missing funding ranges" in (a / "report.md").read_text()
    duplicate = runner.invoke(app, ["backtest", "run", str(toml)])
    assert duplicate.exit_code == 0  # clock now points to a third timestamp
    duplicate = runner.invoke(app, ["backtest", "run", str(toml)])
    assert duplicate.exit_code == 1 and "already exists" in duplicate.output


def test_atomic_publish_never_leaves_partial_report(tmp_path, monkeypatch):
    result = BacktestEngine(
        config(), store([candle(d) for d in range(-1, 4)]), universe(), {"BTCUSDT": FINE}, {}
    ).run(BuyAndHoldBTC())

    def fail(*args):
        raise OSError("simulated disk error")

    monkeypatch.setattr(reporting, "_charts", fail)
    with pytest.raises(OSError, match="simulated disk error"):
        reporting.save_report(
            tmp_path, config(), "", result, calculate_metrics(result), [], ManualClock(START)
        )
    assert list(tmp_path.iterdir()) == []


def test_members_is_sorted_historical_union_including_removed_symbols(tmp_path, monkeypatch):
    save_universe(
        tmp_path, "test", universe(("Z", "A", "B"), {START: ["Z", "A"], START + DAY: ["B", "A"]})
    )
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    out = tmp_path / "members.txt"
    result = CliRunner().invoke(app, ["universe", "members", "test", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text().splitlines() == ["A", "B", "Z"]


def test_metrics_against_two_day_arithmetic():
    equity = [
        {
            "time_ms": START + i * DAY,
            "equity_usd": value,
            "fees_usd": 0,
            "slippage_usd": 0,
            "funding_usd": 0,
            "gross_exposure": 1,
            "net_exposure": 1,
        }
        for i, value in enumerate((100, 110, 99))
    ]
    metrics = calculate_metrics(BacktestResult([], [], equity, [], None))
    assert metrics["total_return_frac"] == pytest.approx(-0.01)
    assert metrics["cagr_frac"] == pytest.approx(0.99 ** (365 / 2) - 1)
    assert metrics["annual_volatility_frac"] == pytest.approx(math.sqrt(0.02 * 365))
    assert metrics["sharpe"] == pytest.approx(0, abs=1e-12)
    assert metrics["sortino"] == pytest.approx(0, abs=1e-12)
    assert metrics["max_drawdown_frac"] == pytest.approx(-0.1)
    assert metrics["drawdown_duration_days"] == 1
    assert metrics["by_year"]["2024"]["net_pnl_usd"] == -1
    assert metrics["funding_coverage_frac"] is None


def test_five_years_thirty_symbols_speed():
    symbols = [f"COIN{i}USDT" for i in range(30)]
    rows = [candle(d, symbol=s) for s in symbols for d in range(-1, 5 * 365)]
    data = store(rows)
    univ = universe(symbols)
    rules = {s: replace(FINE, qty_step=Decimal(".001")) for s in symbols}
    started = time.perf_counter()
    result = BacktestEngine(config(5 * 365), data, univ, rules, {}).run(EqualWeightUniverse())
    elapsed = time.perf_counter() - started
    assert len(result.equity) == 5 * 365 + 1
    # A generous CI ceiling catches accidental full-history scans per decision.
    assert elapsed < 8, f"engine took {elapsed:.3f}s for 5 years x 30 symbols"
    print(f"5 years x 30 symbols: {elapsed:.3f}s")


def test_four_hour_metrics_use_midnight_endpoints_including_initial_capital():
    # Six intraday observations in each day: 100 -> 110 -> 99 across UTC midnights.
    values = [100 + i * 10 / 6 for i in range(7)] + [110 - i * 11 / 6 for i in range(1, 7)]
    equity = [
        {
            "time_ms": START + i * Interval.H4.duration_ms,
            "equity_usd": value,
            "fees_usd": 0,
            "slippage_usd": 0,
            "funding_usd": 0,
            "gross_exposure": 1,
            "net_exposure": 1,
        }
        for i, value in enumerate(values)
    ]
    metrics = calculate_metrics(BacktestResult([], [], equity, [], None))
    assert metrics["annual_volatility_frac"] == pytest.approx(math.sqrt(0.02 * 365))
    assert metrics["sharpe"] == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize("values,duration", [([100, 90, 100], 2), ([100, 110, 120], 0)])
def test_drawdown_duration_includes_recovery_endpoint(values, duration):
    equity = [
        {
            "time_ms": START + i * DAY,
            "equity_usd": value,
            "fees_usd": 0,
            "slippage_usd": 0,
            "funding_usd": 0,
            "gross_exposure": 1,
            "net_exposure": 1,
        }
        for i, value in enumerate(values)
    ]
    assert (
        calculate_metrics(BacktestResult([], [], equity, [], None))["drawdown_duration_days"]
        == duration
    )


@pytest.mark.parametrize(
    "edit",
    [
        ('start="2024-01-01"', 'start="2025-01-01"'),
        ('name="offline"', 'name="../unsafe"'),
        ("max_symbol_exposure=1", "max_symbol_exposure=-1"),
    ],
)
def test_invalid_experiment_rejected(tmp_path, edit):
    toml = seed(tmp_path)
    toml.write_text(toml.read_text().replace(*edit), encoding="utf-8")
    with pytest.raises(ValueError):
        load_experiment(toml)
