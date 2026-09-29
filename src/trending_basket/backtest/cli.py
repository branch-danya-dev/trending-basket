"""Offline benchmark experiments and report inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from trending_basket.backtest.config import load_experiment
from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.inputs import load_inputs
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.backtest.periods import BENCHMARKS, authorize_period
from trending_basket.backtest.reporting import _git_state, metric_label, save_report, scalar_table
from trending_basket.clock import SystemClock
from trending_basket.config import load_settings
from trending_basket.strategies.benchmarks import make_strategy

backtest_app = typer.Typer(help="Run and inspect reproducible offline experiments.")


@backtest_app.command("run")
def run_cmd(
    experiment_file: Annotated[Path, typer.Argument()],
    allow_val: Annotated[bool, typer.Option(help="Explicitly access validation data.")] = False,
    allow_holdout: Annotated[
        bool, typer.Option(help="Access holdout/full after confirmation.")
    ] = False,
) -> None:
    try:
        settings = load_settings()
        clock = SystemClock()
        experiment = load_experiment(experiment_file, clock)
        run = experiment.run
        assert run.period is not None
        confirmed = False
        if run.strategy not in BENCHMARKS and run.period in {"holdout", "full"} and allow_holdout:
            confirmed = typer.confirm(
                "Access protected holdout data? This will be permanently logged."
            )
        access = authorize_period(
            experiment=run.name,
            strategy=run.strategy,
            period=run.period,
            allow_val=allow_val,
            allow_holdout=allow_holdout,
            confirmed=confirmed,
            reports_dir=settings.reports_dir,
            clock=clock,
            commit=_git_state()["commit"],
        )
        inputs = load_inputs(settings.data_dir, experiment)
        result = BacktestEngine(
            experiment, inputs.data, inputs.universe, inputs.rules, inputs.funding
        ).run(make_strategy(run.strategy, experiment.strategy_params, run.interval), access)
        metrics = calculate_metrics(result)
        destination = save_report(
            settings.reports_dir,
            experiment,
            experiment_file.read_text(encoding="utf-8"),
            result,
            metrics,
            inputs.provenance,
            clock,
        )
    except (OSError, ValueError, ArithmeticError) as exc:
        typer.echo(f"backtest failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"report: {destination}")
    typer.echo(scalar_table(metrics))
    for warning in metrics["warnings"]:
        typer.echo(f"WARNING: {warning}")


@backtest_app.command("show")
def show_cmd(run_dir: Annotated[Path, typer.Argument()]) -> None:
    try:
        typer.echo((run_dir / "report.md").read_text(encoding="utf-8"))
    except OSError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@backtest_app.command("compare")
def compare_cmd(run_dirs: Annotated[list[Path], typer.Argument()]) -> None:
    try:
        metrics = [json.loads((d / "metrics.json").read_text(encoding="utf-8")) for d in run_dirs]
        typer.echo("| Run | Quantity mode |")
        typer.echo("|---|---|")
        for directory, values in zip(run_dirs, metrics, strict=True):
            typer.echo(f"| {directory.name} | {values.get('quantity_mode', 'exchange')} |")
        typer.echo("")
        keys = sorted({k for m in metrics for k, v in m.items() if not isinstance(v, (dict, list))})
        typer.echo("| Metric | " + " | ".join(d.name for d in run_dirs) + " |")
        typer.echo("|---|" + "---:|" * len(run_dirs))
        for key in keys:
            typer.echo(
                f"| {metric_label(key)} | " + " | ".join(str(m.get(key)) for m in metrics) + " |"
            )
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
