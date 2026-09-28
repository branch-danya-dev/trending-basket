"""CLI for candidate discovery and monthly research universes."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from trending_basket.clock import SystemClock
from trending_basket.config import load_settings
from trending_basket.universe.candidates import DEFAULT_EXCLUSIONS, candidate_pool
from trending_basket.universe.selection import SelectionParameters
from trending_basket.universe.storage import build_universe, load_universe

universe_app = typer.Typer(help="Build and inspect monthly symbol universes.")


@universe_app.command("candidates")
def candidates_cmd(
    out: Annotated[Path, typer.Option("--out", help="Output symbols file")],
    exclusions_file: Annotated[Path, typer.Option("--exclusions-file")] = DEFAULT_EXCLUSIONS,
) -> None:
    try:
        pool = candidate_pool(load_settings().data_dir, exclusions_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(out.name + ".tmp")
        try:
            temporary.write_text(
                "".join(f"{symbol}\n" for symbol in pool.symbols), encoding="utf-8"
            )
            os.replace(temporary, out)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"candidates={len(pool.symbols)}, excluded={len(pool.excluded)}, "
        f"snapshot={pool.snapshot.name}"
    )


def _date_ms(value: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise typer.BadParameter("date must be YYYY-MM-DD, UTC") from exc
    return int(parsed.timestamp() * 1000)


def _date_string(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=UTC).date().isoformat()


@universe_app.command("build")
def build_cmd(
    name: Annotated[str, typer.Option("--name")],
    since: Annotated[str, typer.Option("--since", help="First month, YYYY-MM-01 UTC")],
    top: Annotated[int, typer.Option("--top", min=1)] = 15,
    min_history_days: Annotated[int, typer.Option("--min-history-days", min=1)] = 120,
    turnover_window: Annotated[int, typer.Option("--turnover-window", min=1)] = 30,
    min_turnover: Annotated[float, typer.Option("--min-turnover", min=0)] = 10_000_000,
    exclusions_file: Annotated[Path, typer.Option("--exclusions-file")] = DEFAULT_EXCLUSIONS,
) -> None:
    since_ms = _date_ms(since)
    try:
        universe = build_universe(
            data_dir=load_settings().data_dir,
            name=name,
            since_ms=since_ms,
            parameters=SelectionParameters(top, min_history_days, turnover_window, min_turnover),
            clock=SystemClock(),
            exclusions_path=exclusions_file,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from exc
    meta = universe.metadata
    typer.echo(
        f"{name}: months={len(meta['rebalance_times_ms'])}, rows={len(universe.table)}, "
        f"candidates={meta['candidate_count']}"
    )
    typer.echo(
        f"missing caches ({len(meta['missing_caches'])}): {','.join(meta['missing_caches']) or '-'}"
    )
    typer.echo(
        "underfilled months: "
        + (",".join(_date_string(at) for at in meta["underfilled_months_ms"]) or "-")
    )
    typer.echo("survivorship_bias=true: " + meta["survivorship_bias_explanation"])


@universe_app.command("report")
def report_cmd(name: Annotated[str, typer.Argument()]) -> None:
    try:
        universe = load_universe(load_settings().data_dir, name)
    except (OSError, ValueError) as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from exc
    previous: set[str] = set()
    typer.echo("month | count | entered | exited | min_turnover_usd | median_turnover_usd | flags")
    for at in universe.metadata["rebalance_times_ms"]:
        current = set(universe.universe_at(at))
        selected = universe.table.loc[universe.table["rebalance_time_ms"] == at]
        entered = ",".join(sorted(current - previous)) or "-"
        exited = ",".join(sorted(previous - current)) or "-"
        minimum = f"{selected['median_turnover_usd'].min():.2f}" if current else "-"
        median = f"{selected['median_turnover_usd'].median():.2f}" if current else "-"
        flag = "underfilled" if at in universe.metadata["underfilled_months_ms"] else "-"
        typer.echo(
            f"{_date_string(at)} | {len(current)} | {entered} | {exited} | "
            f"{minimum} | {median} | {flag}"
        )
        previous = current
    typer.echo("missing caches: " + (",".join(universe.metadata["missing_caches"]) or "-"))
    typer.echo("survivorship_bias=true: " + universe.metadata["survivorship_bias_explanation"])


@universe_app.command("show")
def show_cmd(
    name: Annotated[str, typer.Argument()],
    at: Annotated[str, typer.Option("--at", help="Exact rebalance date, YYYY-MM-01 UTC")],
) -> None:
    at_ms = _date_ms(at)
    try:
        universe = load_universe(load_settings().data_dir, name)
        if at_ms not in universe.metadata["rebalance_times_ms"]:
            raise ValueError("no scheduled rebalance on this date")
    except (OSError, ValueError) as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from exc
    selected = universe.table.loc[universe.table["rebalance_time_ms"] == at_ms].sort_values("rank")
    typer.echo(selected.to_string(index=False) if not selected.empty else "empty universe")
