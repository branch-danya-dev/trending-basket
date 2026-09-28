"""Tests for the local parquet cache: sync, idempotency, validation, gaps."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from bybit_test_support import FakeBybitClient, make_candle

from trending_basket.clock import ManualClock
from trending_basket.data.cache import (
    find_gaps,
    funding_path,
    instruments_path,
    klines_dir,
    klines_path,
    sync_funding,
    sync_instruments,
    sync_klines,
    validate_klines,
)
from trending_basket.domain.types import Candle, FundingRate, InstrumentInfo, Interval

DAY_MS = Interval.D1.duration_ms
BASE_MS = 1704067200000  # 2024-01-01T00:00:00Z


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- sync_klines --------------------------------------------------------------


def test_sync_klines_first_sync(tmp_path: Path) -> None:
    candles = [make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(5)]
    client = FakeBybitClient(server_now_ms=BASE_MS + 5 * DAY_MS, all_candles=candles)

    result = sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    assert result.added_rows == 5
    assert result.rejected_rows == 0
    df = pd.read_parquet(klines_path(tmp_path, Interval.D1, "BTCUSDT"))
    assert list(df["open_time_ms"]) == [BASE_MS + i * DAY_MS for i in range(5)]


def test_sync_klines_repeat_sync_is_noop(tmp_path: Path) -> None:
    candles = [make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(5)]
    client = FakeBybitClient(server_now_ms=BASE_MS + 5 * DAY_MS, all_candles=candles)
    sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )
    path = klines_path(tmp_path, Interval.D1, "BTCUSDT")
    hash_before = _file_hash(path)

    result = sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    assert result.added_rows == 0
    assert _file_hash(path) == hash_before


def test_sync_klines_backfills_tail(tmp_path: Path) -> None:
    candles = [make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(5)]
    client = FakeBybitClient(server_now_ms=BASE_MS + 5 * DAY_MS, all_candles=candles)
    sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    client.all_candles.extend(make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(5, 8))
    client.server_now_ms = BASE_MS + 8 * DAY_MS

    result = sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    assert result.added_rows == 3
    df = pd.read_parquet(klines_path(tmp_path, Interval.D1, "BTCUSDT"))
    assert len(df) == 8
    assert list(df["open_time_ms"]) == [BASE_MS + i * DAY_MS for i in range(8)]


def test_sync_klines_backfills_head_when_since_predates_cache(tmp_path: Path) -> None:
    candles = [make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(3, 6)]
    client = FakeBybitClient(server_now_ms=BASE_MS + 6 * DAY_MS, all_candles=candles)
    sync_klines(
        data_dir=tmp_path,
        client=client,
        symbol="BTCUSDT",
        interval=Interval.D1,
        since_ms=BASE_MS + 3 * DAY_MS,
    )

    client.all_candles.extend(make_candle("BTCUSDT", BASE_MS + i * DAY_MS) for i in range(3))

    result = sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    assert result.added_rows == 3
    df = pd.read_parquet(klines_path(tmp_path, Interval.D1, "BTCUSDT"))
    assert len(df) == 6
    assert df["open_time_ms"].iloc[0] == BASE_MS


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    client = FakeBybitClient(
        server_now_ms=BASE_MS + DAY_MS, all_candles=[make_candle("BTCUSDT", BASE_MS)]
    )

    sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    assert list(klines_dir(tmp_path, Interval.D1).glob("*.tmp")) == []


def test_sync_klines_rejects_invalid_existing_rows_without_writing_them(tmp_path: Path) -> None:
    path = klines_path(tmp_path, Interval.D1, "BTCUSDT")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "open_time_ms": [BASE_MS],
            "open": [100.0],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
            "volume": [-5.0],  # invalid: negative volume
            "turnover": [1000.0],
        }
    ).to_parquet(path, index=False)

    client = FakeBybitClient(
        server_now_ms=BASE_MS + 2 * DAY_MS,
        all_candles=[make_candle("BTCUSDT", BASE_MS + DAY_MS)],
    )

    result = sync_klines(
        data_dir=tmp_path, client=client, symbol="BTCUSDT", interval=Interval.D1, since_ms=BASE_MS
    )

    assert result.rejected_rows == 1
    df = pd.read_parquet(path)
    assert list(df["open_time_ms"]) == [BASE_MS + DAY_MS]


# --- sync_funding --------------------------------------------------------------


def test_sync_funding_first_sync_then_idempotent_repeat(tmp_path: Path) -> None:
    rates = [
        FundingRate(symbol="BTCUSDT", funding_time_ms=BASE_MS + i * 28_800_000, rate_frac=0.0001)
        for i in range(3)
    ]
    client = FakeBybitClient(server_now_ms=BASE_MS + 3 * 28_800_000, all_funding=rates)

    result = sync_funding(data_dir=tmp_path, client=client, symbol="BTCUSDT", since_ms=BASE_MS)
    assert result.added_rows == 3

    path = funding_path(tmp_path, "BTCUSDT")
    hash_before = _file_hash(path)

    result2 = sync_funding(data_dir=tmp_path, client=client, symbol="BTCUSDT", since_ms=BASE_MS)
    assert result2.added_rows == 0
    assert _file_hash(path) == hash_before


# --- sync_instruments --------------------------------------------------------------


def _make_instrument(symbol: str = "BTCUSDT") -> InstrumentInfo:
    return InstrumentInfo(
        symbol=symbol,
        contract_type="LinearPerpetual",
        status="Trading",
        base_coin="BTC",
        quote_coin="USDT",
        launch_time_ms=BASE_MS,
        delivery_time_ms=0,
        funding_interval_ms=28_800_000,
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        max_order_qty=Decimal("100"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
    )


def test_sync_instruments_writes_todays_snapshot(tmp_path: Path) -> None:
    client = FakeBybitClient(server_now_ms=BASE_MS, all_instruments=[_make_instrument()])

    result = sync_instruments(data_dir=tmp_path, client=client, clock=ManualClock(BASE_MS))

    assert result.added_rows == 1
    df = pd.read_parquet(instruments_path(tmp_path, date(2024, 1, 1)))
    assert df["tick_size"].iloc[0] == "0.10"


def test_sync_instruments_does_not_overwrite_older_snapshots(tmp_path: Path) -> None:
    client = FakeBybitClient(server_now_ms=BASE_MS, all_instruments=[_make_instrument()])
    sync_instruments(data_dir=tmp_path, client=client, clock=ManualClock(BASE_MS))
    day1_path = instruments_path(tmp_path, date(2024, 1, 1))
    day1_hash = _file_hash(day1_path)

    sync_instruments(data_dir=tmp_path, client=client, clock=ManualClock(BASE_MS + DAY_MS))

    assert _file_hash(day1_path) == day1_hash
    assert instruments_path(tmp_path, date(2024, 1, 2)).is_file()


def test_sync_instruments_preserves_asset_classification(tmp_path: Path) -> None:
    stock = replace(
        _make_instrument("AAPLUSDT"),
        base_coin="AAPL",
        symbol_type="stock",
        market_region="US",
        underlying_ticker="AAPL",
    )
    client = FakeBybitClient(server_now_ms=BASE_MS, all_instruments=[stock])
    sync_instruments(data_dir=tmp_path, client=client, clock=ManualClock(BASE_MS))
    frame = pd.read_parquet(instruments_path(tmp_path, date(2024, 1, 1)))
    assert frame.loc[0, ["symbol_type", "market_region", "underlying_ticker"]].tolist() == [
        "stock",
        "US",
        "AAPL",
    ]


# --- find_gaps --------------------------------------------------------------


def test_find_gaps_detects_single_gap() -> None:
    df = pd.DataFrame({"open_time_ms": [BASE_MS, BASE_MS + 3 * DAY_MS]})
    assert find_gaps(df, Interval.D1) == [(BASE_MS + DAY_MS, BASE_MS + 2 * DAY_MS)]


def test_find_gaps_detects_multiple_gaps() -> None:
    df = pd.DataFrame(
        {
            "open_time_ms": [
                BASE_MS,
                BASE_MS + 2 * DAY_MS,
                BASE_MS + 3 * DAY_MS,
                BASE_MS + 6 * DAY_MS,
            ]
        }
    )
    assert find_gaps(df, Interval.D1) == [
        (BASE_MS + DAY_MS, BASE_MS + DAY_MS),
        (BASE_MS + 4 * DAY_MS, BASE_MS + 5 * DAY_MS),
    ]


def test_find_gaps_empty_when_contiguous() -> None:
    df = pd.DataFrame({"open_time_ms": [BASE_MS, BASE_MS + DAY_MS, BASE_MS + 2 * DAY_MS]})
    assert find_gaps(df, Interval.D1) == []


def test_find_gaps_empty_when_no_rows() -> None:
    assert find_gaps(pd.DataFrame({"open_time_ms": []}), Interval.D1) == []


# --- validate_klines vs. Candle consistency ----------------------------------


_CANDLE_ROWS: list[dict[str, float]] = [
    {
        "open_time_ms": BASE_MS,
        "open": 100.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
        "volume": 10.0,
        "turnover": 1000.0,
    },  # valid
    {
        "open_time_ms": BASE_MS,
        "open": -1.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
        "volume": 10.0,
        "turnover": 1000.0,
    },  # invalid: non-positive open
    {
        "open_time_ms": BASE_MS,
        "open": 100.0,
        "high": 95.0,
        "low": 95.0,
        "close": 102.0,
        "volume": 10.0,
        "turnover": 1000.0,
    },  # invalid: high < close
    {
        "open_time_ms": BASE_MS,
        "open": 100.0,
        "high": 105.0,
        "low": 103.0,
        "close": 102.0,
        "volume": 10.0,
        "turnover": 1000.0,
    },  # invalid: low > close
    {
        "open_time_ms": BASE_MS,
        "open": 100.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
        "volume": -1.0,
        "turnover": 1000.0,
    },  # invalid: negative volume
    {
        "open_time_ms": BASE_MS + 1,
        "open": 100.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
        "volume": 10.0,
        "turnover": 1000.0,
    },  # invalid: misaligned open_time_ms
]


@pytest.mark.parametrize("row", _CANDLE_ROWS)
def test_validate_klines_matches_candle_post_init(row: dict[str, float]) -> None:
    df = pd.DataFrame({k: [v] for k, v in row.items()})
    vectorized_valid = bool(validate_klines(df, Interval.D1).iloc[0])

    try:
        Candle(symbol="BTCUSDT", interval=Interval.D1, **row)  # type: ignore[arg-type]
        candle_valid = True
    except ValueError:
        candle_valid = False

    assert vectorized_valid == candle_valid
