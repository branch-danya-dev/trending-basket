"""T005 selection and verdict commands consume reports, never run strategies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated

import typer

from trending_basket.backtest.config import load_experiment
from trending_basket.backtest.reporting import _git_state
from trending_basket.clock import SystemClock
from trending_basket.research.decisions import (
    CANDIDATES,
    holdout_verdict,
    read_report,
    select_candidate,
    without_period,
    write_decision,
)
from trending_basket.research.risk import risk_level_cmd

research_app = typer.Typer(help="Apply preregistered T005 rules to saved reports.")
research_app.command("risk-level")(risk_level_cmd)


@research_app.command("select")
def select_cmd(
    period: Annotated[str, typer.Option()],
    candidates: Annotated[
        list[Path], typer.Option(help="Repeat --candidates RUN_DIR for each ID.")
    ],
    output: Annotated[Path, typer.Option()] = Path("docs/reports/T005-selection.json"),
) -> None:
    try:
        if period != "val":
            raise ValueError("selection is only permitted on val")
        reports = [read_report(path, "val", "trend_basket") for path in candidates]
        by_id = {row["id"]: row for row in reports}
        if len(reports) != len(CANDIDATES) or set(by_id) != CANDIDATES:
            raise ValueError("exactly one report for each of V1x,V2x,V3x,V4x,H1,H2,H3 is required")
        for ident, report in by_id.items():
            registered = load_experiment(Path("experiments") / f"{ident}.toml")
            if without_period(report["resolved_config"]) != without_period(
                registered.model_dump(mode="json")
            ):
                raise ValueError(f"{ident}: configuration differs from registered T004b candidate")
        decision = select_candidate({ident: row["metrics"] for ident, row in by_id.items()})
        decision.update(sources=by_id, created_at_ms=SystemClock().now_ms(), git=_git_state())
        write_decision(output, decision)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"selection failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"selected: {decision['selected_id']}; holdout_allowed: {decision['holdout_allowed']}"
    )
    typer.echo(f"decision: {output}")


@research_app.command("holdout-verdict")
def verdict_cmd(
    candidate: Annotated[Path, typer.Option(help="Selected candidate's holdout report.")],
    btc: Annotated[Path, typer.Option(help="BTC exact holdout report for the same window.")],
    selection: Annotated[Path, typer.Option()] = Path("docs/reports/T005-selection.json"),
    output: Annotated[Path, typer.Option()] = Path("docs/reports/T005-holdout.json"),
) -> None:
    try:
        selection_bytes = selection.read_bytes()
        selected = json.loads(selection_bytes)
        rows = selected["candidates"]
        if len(rows) != len(CANDIDATES) or {row["id"] for row in rows} != CANDIDATES:
            raise ValueError("invalid registered candidate set in selection")
        recomputed = select_candidate({row["id"]: row["metrics"] for row in rows})
        if (
            not recomputed["holdout_allowed"]
            or recomputed["selected_id"] != selected["selected_id"]
        ):
            raise ValueError("selection does not authorize this holdout")
        strategy_report = read_report(candidate, "holdout", "trend_basket")
        btc_report = read_report(btc, "holdout", "buy_and_hold_btc")
        if strategy_report["id"] != selected["selected_id"]:
            raise ValueError("holdout candidate differs from selection")
        chosen_config = selected["sources"][selected["selected_id"]]["resolved_config"]
        if without_period(strategy_report["resolved_config"]) != without_period(chosen_config):
            raise ValueError("holdout parameters differ from selected validation candidate")
        candidate_run = strategy_report["resolved_config"]["run"]
        btc_run = btc_report["resolved_config"]["run"]
        if any(candidate_run[key] != btc_run[key] for key in ("start", "end")):
            raise ValueError("candidate and BTC must cover the same holdout window")
        decision = holdout_verdict(strategy_report["metrics"], btc_report["metrics"])
        decision.update(
            selected_id=selected["selected_id"],
            period="holdout",
            start=candidate_run["start"],
            end=candidate_run["end"],
            candidate=strategy_report,
            btc=btc_report,
            selection_sha256=hashlib.sha256(selection_bytes).hexdigest(),
            created_at_ms=SystemClock().now_ms(),
            git=_git_state(),
        )
        write_decision(output, decision)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"verdict failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"passed: {decision['passed']}; next_step: {decision['next_step']}")
    typer.echo(f"decision: {output}")
