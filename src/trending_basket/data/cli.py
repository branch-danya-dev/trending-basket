"""CLI commands for the data layer: `tb data sync|check|show`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from trending_basket.clock import SystemClock
from trending_basket.config import load_settings
from trending_basket.data.bybit_client import BybitAPIError, BybitPublicClient, build_client
from trending_basket.data.cache import (
    find_gaps,
    klines_dir,
    klines_path,
    sync_funding,
    sync_instruments,
    sync_klines,
    validate_klines,
)
from trending_basket.domain.types import Interval

data_app = typer.Typer(help="Market data sync and inspection.")
sync_app = typer.Typer(help="Synchronize the local cache from Bybit.")
data_app.add_typer(sync_app, name="sync")


def get_client() -> BybitPublicClient:
    """Factory for the Bybit client used by these commands. Tests monkeypatch this."""
    return build_client(load_settings())


def _resolve_symbols(symbols: str | None, symbols_file: Path | None) -> list[str]:
    if symbols and symbols_file:
        raise typer.BadParameter("specify either --symbols or --symbols-file, not both")
    if symbols:
        return [s.strip() for s in symbols.split(",") if s.strip()]
    if symbols_file:
        if not symbols_file.is_file():
            raise typer.BadParameter(f"symbols file not found: {symbols_file}")
        return [
            line.strip()
            for line in symbols_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    raise typer.BadParameter("no symbols given: use --symbols or --symbols-file")


def _parse_interval(value: str) -> Interval:
    try:
        return Interval(value)
    except ValueError as exc:
        allowed = ", ".join(i.value for i in Interval)
        raise typer.BadParameter(f"--interval must be one of: {allowed}") from exc


def _parse_since_ms(value: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise typer.BadParameter(f"--since must be YYYY-MM-DD, got {value!r}") from exc
    return int(parsed.timestamp() * 1000)


def _discover_symbols(data_dir: Path, intervals: list[Interval]) -> list[str]:
    symbols: set[str] = set()
    for interval in intervals:
        directory = klines_dir(data_dir, interval)
        if directory.is_dir():
            symbols.update(p.stem for p in directory.glob("*.parquet"))
    return sorted(symbols)


@sync_app.command("klines")
def sync_klines_cmd(
    interval: Annotated[str, typer.Option("--interval", help="4h or 1d")],
    since: Annotated[str, typer.Option("--since", help="YYYY-MM-DD, UTC")],
    symbols: Annotated[
        str | None, typer.Option("--symbols", help="Comma-separated symbols")
    ] = None,
    symbols_file: Annotated[
        Path | None, typer.Option("--symbols-file", help="One symbol per line")
    ] = None,
) -> None:
    """Sync klines for the given symbols. Continues past per-symbol errors."""
    symbol_list = _resolve_symbols(symbols, symbols_file)
    interval_enum = _parse_interval(interval)
    since_ms = _parse_since_ms(since)
    settings = load_settings()
    client = get_client()

    had_error = False
    for symbol in symbol_list:
        try:
            result = sync_klines(
                data_dir=settings.data_dir,
                client=client,
                symbol=symbol,
                interval=interval_enum,
                since_ms=since_ms,
            )
        except BybitAPIError as exc:
            had_error = True
            typer.echo(f"{symbol}: ERROR {exc}", err=True)
            continue

        path = klines_path(settings.data_dir, interval_enum, symbol)
        cached = pd.read_parquet(path) if path.is_file() else pd.DataFrame()
        gaps = find_gaps(cached, interval_enum)
        typer.echo(
            f"{symbol}: +{result.added_rows} rows, "
            f"range=[{result.first_time_ms}, {result.last_time_ms}], "
            f"gaps={len(gaps)}, rejected={result.rejected_rows}"
        )

    raise typer.Exit(code=1 if had_error else 0)


@sync_app.command("funding")
def sync_funding_cmd(
    since: Annotated[str, typer.Option("--since", help="YYYY-MM-DD, UTC")],
    symbols: Annotated[
        str | None, typer.Option("--symbols", help="Comma-separated symbols")
    ] = None,
    symbols_file: Annotated[
        Path | None, typer.Option("--symbols-file", help="One symbol per line")
    ] = None,
) -> None:
    """Sync funding rate history for the given symbols. Continues past per-symbol errors."""
    symbol_list = _resolve_symbols(symbols, symbols_file)
    since_ms = _parse_since_ms(since)
    settings = load_settings()
    client = get_client()

    had_error = False
    for symbol in symbol_list:
        try:
            result = sync_funding(
                data_dir=settings.data_dir, client=client, symbol=symbol, since_ms=since_ms
            )
        except BybitAPIError as exc:
            had_error = True
            typer.echo(f"{symbol}: ERROR {exc}", err=True)
            continue

        typer.echo(
            f"{symbol}: +{result.added_rows} rows, "
            f"range=[{result.first_time_ms}, {result.last_time_ms}]"
        )

    raise typer.Exit(code=1 if had_error else 0)


@sync_app.command("instruments")
def sync_instruments_cmd() -> None:
    """Save today's (UTC) instrument snapshot. Older snapshots are never overwritten."""
    settings = load_settings()
    client = get_client()

    try:
        result = sync_instruments(data_dir=settings.data_dir, client=client, clock=SystemClock())
    except BybitAPIError as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"saved {result.added_rows} instruments")


@data_app.command("check")
def check_cmd(
    symbols: Annotated[
        str | None, typer.Option("--symbols", help="Comma-separated symbols")
    ] = None,
    symbols_file: Annotated[
        Path | None, typer.Option("--symbols-file", help="One symbol per line")
    ] = None,
    interval: Annotated[
        str | None, typer.Option("--interval", help="4h or 1d; default both")
    ] = None,
) -> None:
    """Report cache coverage, gaps, and validation errors. Exit 1 if any are found."""
    settings = load_settings()
    intervals = [_parse_interval(interval)] if interval else list(Interval)
    symbol_list = (
        _resolve_symbols(symbols, symbols_file)
        if symbols or symbols_file
        else _discover_symbols(settings.data_dir, intervals)
    )

    had_issue = False
    for iv in intervals:
        for symbol in symbol_list:
            path = klines_path(settings.data_dir, iv, symbol)
            if not path.is_file():
                continue
            df = pd.read_parquet(path)
            invalid_count = int((~validate_klines(df, iv)).sum()) if not df.empty else 0
            gaps = find_gaps(df, iv)
            if invalid_count or gaps:
                had_issue = True
            time_range = (
                f"[{int(df['open_time_ms'].min())}, {int(df['open_time_ms'].max())}]"
                if not df.empty
                else "[]"
            )
            typer.echo(
                f"{symbol} {iv.value}: rows={len(df)}, range={time_range}, "
                f"gaps={len(gaps)}, invalid={invalid_count}"
            )

    raise typer.Exit(code=1 if had_issue else 0)


@data_app.command("show")
def show_cmd(
    symbol: Annotated[str, typer.Argument(help="e.g. BTCUSDT")],
    interval: Annotated[str, typer.Option("--interval", help="4h or 1d")] = "1d",
    tail: Annotated[int, typer.Option("--tail", help="Number of most recent rows to print")] = 10,
) -> None:
    """Print the last rows of a symbol's cached klines."""
    settings = load_settings()
    interval_enum = _parse_interval(interval)
    path = klines_path(settings.data_dir, interval_enum, symbol)
    if not path.is_file():
        typer.echo(f"no cached data for {symbol} {interval_enum.value}", err=True)
        raise typer.Exit(code=1)

    df = pd.read_parquet(path)
    typer.echo(df.tail(tail).to_string(index=False))
