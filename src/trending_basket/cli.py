"""Command-line interface for trending-basket."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from trending_basket import __version__
from trending_basket.config import format_settings, load_settings
from trending_basket.data.cli import data_app
from trending_basket.universe.cli import universe_app

app = typer.Typer(help="trending-basket: background trend-following bot for Bybit.")
config_app = typer.Typer(help="Inspect configuration.")
app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(universe_app, name="universe")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration, with secrets masked."""
    settings = load_settings()
    typer.echo(format_settings(settings))


@app.command()
def doctor() -> None:
    """Check that the environment is ready to run trending-basket."""
    results = [_report_check("python >= 3.12", sys.version_info >= (3, 12))]

    try:
        settings = load_settings()
    except ValueError as exc:
        typer.echo(f"config: FAIL ({exc})")
        raise typer.Exit(code=1) from exc

    results.append(_report_check("mode != live", settings.mode != "live"))
    results.append(
        _report_check(
            f"data_dir writable ({settings.data_dir})",
            _check_writable_dir(settings.data_dir),
        )
    )
    results.append(
        _report_check(
            f"reports_dir writable ({settings.reports_dir})",
            _check_writable_dir(settings.reports_dir),
        )
    )

    raise typer.Exit(code=0 if all(results) else 1)


def _report_check(label: str, ok: bool) -> bool:
    typer.echo(f"{label}: {'OK' if ok else 'FAIL'}")
    return ok


def _check_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".tb-doctor-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True
