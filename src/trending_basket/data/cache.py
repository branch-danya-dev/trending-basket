"""Local parquet cache for Bybit market data: klines, funding, instruments.

Layout:
    {data_dir}/bybit/linear/klines/{interval}/{SYMBOL}.parquet
    {data_dir}/bybit/linear/funding/{SYMBOL}.parquet
    {data_dir}/bybit/linear/instruments/{YYYY-MM-DD}.parquet
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import pandas as pd

from trending_basket.clock import Clock
from trending_basket.data.bybit_client import BybitPublicClient
from trending_basket.domain.types import Candle, FundingRate, InstrumentInfo, Interval


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of syncing one symbol's cache file."""

    added_rows: int
    rejected_rows: int
    first_time_ms: int | None
    last_time_ms: int | None


def klines_dir(data_dir: Path, interval: Interval) -> Path:
    return data_dir / "bybit" / "linear" / "klines" / interval.value


def klines_path(data_dir: Path, interval: Interval, symbol: str) -> Path:
    return klines_dir(data_dir, interval) / f"{symbol}.parquet"


def funding_dir(data_dir: Path) -> Path:
    return data_dir / "bybit" / "linear" / "funding"


def funding_path(data_dir: Path, symbol: str) -> Path:
    return funding_dir(data_dir) / f"{symbol}.parquet"


def instruments_dir(data_dir: Path) -> Path:
    return data_dir / "bybit" / "linear" / "instruments"


def instruments_path(data_dir: Path, as_of: date) -> Path:
    return instruments_dir(data_dir) / f"{as_of.isoformat()}.parquet"


def _read_parquet_if_exists(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    return pd.read_parquet(path)


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, path)


def validate_klines(df: pd.DataFrame, interval: Interval) -> pd.Series:
    """Vectorized version of Candle's __post_init__ checks. True where a row is valid."""
    if df.empty:
        return pd.Series([], dtype=bool)
    duration_ms = interval.duration_ms
    prices_positive = (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)
    high_ok = df["high"] >= df[["open", "close"]].max(axis=1)
    low_ok = df["low"] <= df[["open", "close"]].min(axis=1)
    volume_ok = df["volume"] >= 0
    aligned = (df["open_time_ms"] % duration_ms) == 0
    result: pd.Series = prices_positive & high_ok & low_ok & volume_ok & aligned
    return result


def find_gaps(df: pd.DataFrame, interval: Interval) -> list[tuple[int, int]]:
    """Return (gap_start_ms, gap_end_ms) ranges of missing candle slots, first to last row."""
    if df.empty:
        return []
    duration_ms = interval.duration_ms
    times = sorted(int(t) for t in df["open_time_ms"].tolist())
    gaps: list[tuple[int, int]] = []
    for previous, current in pairwise(times):
        expected_next = previous + duration_ms
        if current > expected_next:
            gaps.append((expected_next, current - duration_ms))
    return gaps


def _empty_klines_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time_ms": pd.Series(dtype="int64"),
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
            "turnover": pd.Series(dtype="float64"),
        }
    )


def _candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return _empty_klines_frame()
    return pd.DataFrame(
        {
            "open_time_ms": [c.open_time_ms for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
            "turnover": [c.turnover for c in candles],
        }
    ).astype(
        {
            "open_time_ms": "int64",
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
            "turnover": "float64",
        }
    )


def sync_klines(
    *,
    data_dir: Path,
    client: BybitPublicClient,
    symbol: str,
    interval: Interval,
    since_ms: int,
) -> SyncResult:
    """Backfill from the last saved candle (or since_ms) to the last closed one.

    If since_ms predates the first saved candle, also backfills the start.
    A rerun with nothing new to fetch does not touch the file at all.
    """
    path = klines_path(data_dir, interval, symbol)
    existing = _read_parquet_if_exists(path)
    existing_times: set[int] = (
        {int(t) for t in existing["open_time_ms"].tolist()} if existing is not None else set()
    )

    server_now_ms = client.server_time_ms()
    fetched_frames: list[pd.DataFrame] = []

    if existing_times:
        tail_start_ms = max(existing_times) + interval.duration_ms
        if tail_start_ms <= server_now_ms:
            tail = list(client.iter_klines(symbol, interval, tail_start_ms, server_now_ms))
            fetched_frames.append(_candles_to_frame(tail))

        first_existing_ms = min(existing_times)
        if since_ms < first_existing_ms:
            head = list(client.iter_klines(symbol, interval, since_ms, first_existing_ms - 1))
            fetched_frames.append(_candles_to_frame(head))
    else:
        full = list(client.iter_klines(symbol, interval, since_ms, server_now_ms))
        fetched_frames.append(_candles_to_frame(full))

    fetched_df = (
        pd.concat(fetched_frames, ignore_index=True) if fetched_frames else _empty_klines_frame()
    )
    combined = (
        pd.concat([existing, fetched_df], ignore_index=True) if existing is not None else fetched_df
    )
    combined = (
        combined.drop_duplicates(subset="open_time_ms", keep="last")
        .sort_values("open_time_ms")
        .reset_index(drop=True)
    )

    valid_mask = validate_klines(combined, interval)
    rejected_rows = int((~valid_mask).sum())
    clean = combined[valid_mask].reset_index(drop=True)

    new_times = {int(t) for t in clean["open_time_ms"].tolist()} - existing_times
    added_rows = len(new_times)

    if existing is None or added_rows > 0:
        _atomic_write_parquet(clean, path)

    return SyncResult(
        added_rows=added_rows,
        rejected_rows=rejected_rows,
        first_time_ms=int(clean["open_time_ms"].iloc[0]) if not clean.empty else None,
        last_time_ms=int(clean["open_time_ms"].iloc[-1]) if not clean.empty else None,
    )


def _empty_funding_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "funding_time_ms": pd.Series(dtype="int64"),
            "rate_frac": pd.Series(dtype="float64"),
        }
    )


def _funding_to_frame(rates: list[FundingRate]) -> pd.DataFrame:
    if not rates:
        return _empty_funding_frame()
    return pd.DataFrame(
        {
            "funding_time_ms": [r.funding_time_ms for r in rates],
            "rate_frac": [r.rate_frac for r in rates],
        }
    ).astype({"funding_time_ms": "int64", "rate_frac": "float64"})


def sync_funding(
    *,
    data_dir: Path,
    client: BybitPublicClient,
    symbol: str,
    since_ms: int,
) -> SyncResult:
    """Backfill funding rate history the same way sync_klines backfills candles."""
    path = funding_path(data_dir, symbol)
    existing = _read_parquet_if_exists(path)
    existing_times: set[int] = (
        {int(t) for t in existing["funding_time_ms"].tolist()} if existing is not None else set()
    )

    server_now_ms = client.server_time_ms()
    fetched_frames: list[pd.DataFrame] = []

    if existing_times:
        tail_start_ms = max(existing_times) + 1
        if tail_start_ms <= server_now_ms:
            tail = client.fetch_funding_history(symbol, tail_start_ms, server_now_ms)
            fetched_frames.append(_funding_to_frame(tail))

        first_existing_ms = min(existing_times)
        if since_ms < first_existing_ms:
            head = client.fetch_funding_history(symbol, since_ms, first_existing_ms - 1)
            fetched_frames.append(_funding_to_frame(head))
    else:
        full = client.fetch_funding_history(symbol, since_ms, server_now_ms)
        fetched_frames.append(_funding_to_frame(full))

    fetched_df = (
        pd.concat(fetched_frames, ignore_index=True) if fetched_frames else _empty_funding_frame()
    )
    combined = (
        pd.concat([existing, fetched_df], ignore_index=True) if existing is not None else fetched_df
    )
    clean = (
        combined.drop_duplicates(subset="funding_time_ms", keep="last")
        .sort_values("funding_time_ms")
        .reset_index(drop=True)
    )

    new_times = {int(t) for t in clean["funding_time_ms"].tolist()} - existing_times
    added_rows = len(new_times)

    if existing is None or added_rows > 0:
        _atomic_write_parquet(clean, path)

    return SyncResult(
        added_rows=added_rows,
        rejected_rows=0,
        first_time_ms=int(clean["funding_time_ms"].iloc[0]) if not clean.empty else None,
        last_time_ms=int(clean["funding_time_ms"].iloc[-1]) if not clean.empty else None,
    )


def _instruments_to_frame(instruments: list[InstrumentInfo]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [i.symbol for i in instruments],
            "contract_type": [i.contract_type for i in instruments],
            "status": [i.status for i in instruments],
            "base_coin": [i.base_coin for i in instruments],
            "quote_coin": [i.quote_coin for i in instruments],
            "launch_time_ms": [i.launch_time_ms for i in instruments],
            "delivery_time_ms": [i.delivery_time_ms for i in instruments],
            "funding_interval_ms": [i.funding_interval_ms for i in instruments],
            "tick_size": [str(i.tick_size) for i in instruments],
            "min_order_qty": [str(i.min_order_qty) for i in instruments],
            "max_order_qty": [str(i.max_order_qty) for i in instruments],
            "qty_step": [str(i.qty_step) for i in instruments],
            "min_notional_value": [str(i.min_notional_value) for i in instruments],
        }
    )


def _utc_date_from_ms(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date()


def sync_instruments(*, data_dir: Path, client: BybitPublicClient, clock: Clock) -> SyncResult:
    """Save today's (UTC) instrument snapshot. Snapshots from other days are never touched."""
    as_of = _utc_date_from_ms(clock.now_ms())
    path = instruments_path(data_dir, as_of)

    instruments = client.fetch_instruments()
    df = _instruments_to_frame(instruments)
    _atomic_write_parquet(df, path)

    return SyncResult(added_rows=len(df), rejected_rows=0, first_time_ms=None, last_time_ms=None)
