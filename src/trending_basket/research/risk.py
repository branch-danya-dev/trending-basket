"""One preregistered risk scaling from dev/val returns, followed by three checks."""

from __future__ import annotations

import hashlib
import math
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import typer

from trending_basket.backtest.config import Experiment, load_experiment
from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.inputs import load_inputs
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.backtest.periods import authorize_period
from trending_basket.backtest.reporting import _git_state, save_report
from trending_basket.clock import SystemClock
from trending_basket.config import load_settings
from trending_basket.research.decisions import read_report, without_period, write_decision
from trending_basket.strategies.benchmarks import make_strategy


def risk_from_volatility(volatility_frac: float) -> float:
    if not math.isfinite(volatility_frac) or volatility_frac <= 0:
        raise ValueError("positive finite dev/val volatility required")
    raw = min(Decimal("0.01"), Decimal("0.005") * Decimal("0.12") / Decimal(str(volatility_frac)))
    step = Decimal("0.0005")
    result = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    if result <= 0:
        raise ValueError("risk rounds to zero")
    return float(result)


def baseline_returns(directory: Path, period: str) -> tuple[pd.Series[float], dict[str, Any]]:
    if period not in {"dev", "val"}:
        raise ValueError("risk estimation forbids holdout")
    report = read_report(directory, period, "trend_basket")
    base = load_experiment(Path("experiments/V1x.toml"))
    if without_period(report["resolved_config"]) != without_period(base.model_dump(mode="json")):
        raise ValueError("risk baseline must be unchanged V1x exact $10000")
    config = Experiment.model_validate(report["resolved_config"])
    frame = pd.read_parquet(directory / "equity.parquet")
    expected = list(range(config.run.start_ms, config.run.end_ms + 1, 86400000))
    if frame["time_ms"].tolist() != expected or (frame["equity_usd"] <= 0).any():
        raise ValueError("baseline equity dates or capital invalid")
    returns = frame["equity_usd"].pct_change().dropna()
    report["equity_sha256"] = hashlib.sha256(
        (directory / "equity.parquet").read_bytes()
    ).hexdigest()
    return returns, report


def risk_level_cmd(
    dev: Annotated[Path, typer.Option()],
    val: Annotated[Path, typer.Option()],
    allow_val: Annotated[bool, typer.Option()] = False,
    output: Annotated[Path, typer.Option()] = Path("docs/reports/T006-risk-level.json"),
) -> None:
    try:
        if not allow_val:
            raise ValueError("risk-level requires --allow-val; holdout is never allowed")
        if output.exists():
            raise ValueError("risk decision already exists; no automatic retuning")
        dev_returns, dev_source = baseline_returns(dev, "dev")
        val_returns, val_source = baseline_returns(val, "val")
        combined = pd.concat([dev_returns, val_returns], ignore_index=True)
        volatility = float(combined.std(ddof=1) * math.sqrt(365))
        risk = risk_from_volatility(volatility)
        settings, clock = load_settings(), SystemClock()
        runs: dict[str, Any] = {}
        for period, mode, capital in [
            ("dev", "exact", 10000),
            ("val", "exact", 10000),
            ("val", "exchange", 1000),
        ]:
            base_file = Path(
                "experiments/val/V1x.toml" if period == "val" else "experiments/V1x.toml"
            )
            base = load_experiment(base_file)
            raw = base.model_dump(mode="json")
            ident = f"T006-V1x-{period}-{mode}"
            raw["run"].update(name=ident, initial_capital_usd=capital)
            raw["strategy_params"]["risk_per_symbol_frac"] = risk
            raw["execution"]["quantity_mode"] = mode
            config = Experiment.model_validate(raw)
            toml = (
                base_file.read_text(encoding="utf-8")
                .replace('name = "V1x"', f'name = "{ident}"')
                .replace("risk_per_symbol_frac = 0.005", f"risk_per_symbol_frac = {risk}")
                .replace("initial_capital_usd = 10000", f"initial_capital_usd = {capital}")
                .replace('quantity_mode = "exact"', f'quantity_mode = "{mode}"')
            )
            assert config.run.period is not None
            access = authorize_period(
                experiment=ident,
                strategy="trend_basket",
                period=config.run.period,
                allow_val=allow_val,
                allow_holdout=False,
                confirmed=False,
                reports_dir=settings.reports_dir,
                clock=clock,
                commit=_git_state()["commit"],
            )
            inputs = load_inputs(settings.data_dir, config)
            result = BacktestEngine(
                config, inputs.data, inputs.universe, inputs.rules, inputs.funding
            ).run(
                make_strategy("trend_basket", config.strategy_params, config.run.interval), access
            )
            metrics = calculate_metrics(result)
            directory = save_report(
                settings.reports_dir, config, toml, result, metrics, inputs.provenance, clock
            )
            runs[f"{period}_{mode}"] = dict(
                directory=directory.as_posix(),
                resolved_config=raw,
                metrics={k: v for k, v in metrics.items() if not isinstance(v, (dict, list))},
                sha256={
                    name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                    for name in ("metrics.json", "manifest.json", "equity.parquet")
                },
            )
            typer.echo(f"report: {directory}")
        worst_dd = max(
            abs(runs[p]["metrics"]["max_drawdown_frac"]) for p in ("dev_exact", "val_exact")
        )
        decision = dict(
            rule="ADR-018",
            candidate="V1x",
            baseline_volatility_frac=volatility,
            risk_per_symbol_frac=risk,
            target_volatility_frac=0.12,
            risk_cap_frac=0.01,
            risk_step_frac=0.0005,
            stop_drawdown_frac=max(1.5 * worst_dd, 0.15),
            sources={"dev": dev_source, "val": val_source},
            runs=runs,
            git=_git_state(),
            created_at_ms=clock.now_ms(),
        )
        write_decision(output, decision)
    except (OSError, ValueError, KeyError, ArithmeticError) as exc:
        typer.echo(f"risk-level failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"risk_per_symbol_frac={risk}; stop_drawdown_frac={decision['stop_drawdown_frac']}")
