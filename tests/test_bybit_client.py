"""Tests for BybitPublicClient: parsing, pagination, retries, rate limiting."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from itertools import pairwise

import httpx
import pytest
from bybit_test_support import load_fixture, route_transport

from trending_basket.clock import ManualClock
from trending_basket.data.bybit_client import (
    BybitAPIError,
    BybitPublicClient,
    RateLimiter,
    RetryPolicy,
)
from trending_basket.domain.types import Interval


def _make_client(
    transport: httpx.MockTransport,
    *,
    clock: ManualClock | None = None,
    max_retries: int = 3,
    min_interval_s: float = 0.0,
    random_fn: Callable[[], float] = lambda: 0.0,
    kline_page_limit: int = 1000,
    funding_page_limit: int = 200,
) -> tuple[BybitPublicClient, ManualClock, list[float]]:
    resolved_clock = clock or ManualClock(1_700_000_000_000)
    sleeps: list[float] = []
    http_client = httpx.Client(base_url="https://api.bybit.com", transport=transport)
    rate_limiter = RateLimiter(
        min_interval_s=min_interval_s, clock=resolved_clock, sleep=sleeps.append
    )
    retry_policy = RetryPolicy(max_retries=max_retries, random_fn=random_fn)
    client = BybitPublicClient(
        http_client,
        clock=resolved_clock,
        rate_limiter=rate_limiter,
        retry_policy=retry_policy,
        sleep=sleeps.append,
        kline_page_limit=kline_page_limit,
        funding_page_limit=funding_page_limit,
    )
    return client, resolved_clock, sleeps


# --- Parsing ---------------------------------------------------------------


def test_fetch_klines_parses_descending_response_as_ascending() -> None:
    transport = route_transport(
        {
            "/v5/market/time": [load_fixture("server_time.json")],
            "/v5/market/kline": [load_fixture("kline_1d_page1_recent.json")],
        }
    )
    client, _, _ = _make_client(transport)

    candles = client.fetch_klines("BTCUSDT", Interval.D1, 1704326400000, 1704499200000)

    assert [c.open_time_ms for c in candles] == [1704326400000, 1704412800000]
    assert candles[0].close == 44200.5


def test_fetch_klines_drops_unclosed_last_candle() -> None:
    transport = route_transport(
        {
            "/v5/market/time": [load_fixture("server_time_for_unclosed_4h.json")],
            "/v5/market/kline": [load_fixture("kline_4h_with_unclosed_last.json")],
        }
    )
    client, _, _ = _make_client(transport)

    candles = client.fetch_klines("BTCUSDT", Interval.H4, 1704412800000, 1704445200000)

    assert [c.open_time_ms for c in candles] == [1704412800000, 1704427200000]


# --- Pagination --------------------------------------------------------------


def test_iter_klines_paginates_without_dupes_or_gaps() -> None:
    transport = route_transport(
        {
            "/v5/market/time": [load_fixture("server_time.json")],
            "/v5/market/kline": [
                load_fixture("kline_1d_page1_recent.json"),
                load_fixture("kline_1d_page2_older.json"),
            ],
        }
    )
    client, _, _ = _make_client(transport, kline_page_limit=2)

    candles = list(client.iter_klines("BTCUSDT", Interval.D1, 1704067200000, 1704499200000))

    assert [c.open_time_ms for c in candles] == [
        1704067200000,
        1704153600000,
        1704240000000,
        1704326400000,
        1704412800000,
    ]
    assert len(candles) == len({c.open_time_ms for c in candles})


def test_fetch_funding_history_paginates_backward() -> None:
    transport = route_transport(
        {
            "/v5/market/funding-history": [
                load_fixture("funding_page1_recent.json"),
                load_fixture("funding_page2_older.json"),
            ],
        }
    )
    client, _, _ = _make_client(transport, funding_page_limit=3)

    rates = client.fetch_funding_history("BTCUSDT", 1704067200000, 1704182400000)

    assert [r.funding_time_ms for r in rates] == [
        1704067200000,
        1704096000000,
        1704124800000,
        1704153600000,
        1704182400000,
    ]


def test_fetch_instruments_paginates_via_cursor() -> None:
    transport = route_transport(
        {
            "/v5/market/instruments-info": [
                load_fixture("instruments_page1_cursor.json"),
                load_fixture("instruments_page2_final.json"),
            ],
        }
    )
    client, _, _ = _make_client(transport)

    instruments = client.fetch_instruments()

    assert [i.symbol for i in instruments] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert instruments[0].tick_size == Decimal("0.10")
    assert instruments[0].min_order_qty == Decimal("0.001")
    assert instruments[0].funding_interval_ms == 480 * 60_000


# --- Retries -----------------------------------------------------------------


def test_retries_on_http_429_then_succeeds() -> None:
    transport = route_transport({"/v5/market/time": [(429, {}), load_fixture("server_time.json")]})
    client, _, sleeps = _make_client(transport, max_retries=2)

    assert client.server_time_ms() == 1704499200000
    assert len(sleeps) == 1


def test_retries_on_ret_code_10006_then_succeeds() -> None:
    transport = route_transport(
        {
            "/v5/market/time": [
                load_fixture("rate_limit_10006.json"),
                load_fixture("server_time.json"),
            ]
        }
    )
    client, _, sleeps = _make_client(transport, max_retries=2)

    assert client.server_time_ms() == 1704499200000
    assert len(sleeps) == 1


def test_non_retryable_ret_code_raises_immediately() -> None:
    transport = route_transport({"/v5/market/time": [load_fixture("non_retryable_error.json")]})
    client, _, sleeps = _make_client(transport, max_retries=5)

    with pytest.raises(BybitAPIError) as exc_info:
        client.server_time_ms()

    assert exc_info.value.ret_code == 10001
    assert sleeps == []


def test_retry_exhaustion_raises_bybit_api_error_with_last_reason() -> None:
    transport = route_transport({"/v5/market/time": [load_fixture("rate_limit_10006.json")]})
    client, _, sleeps = _make_client(transport, max_retries=2)

    with pytest.raises(BybitAPIError) as exc_info:
        client.server_time_ms()

    assert exc_info.value.ret_code == 10006
    assert len(sleeps) == 2


def test_backoff_pauses_grow_with_attempt() -> None:
    transport = route_transport({"/v5/market/time": [load_fixture("rate_limit_10006.json")]})
    client, _, sleeps = _make_client(transport, max_retries=3)

    with pytest.raises(BybitAPIError):
        client.server_time_ms()

    assert len(sleeps) == 3
    for earlier, later in pairwise(sleeps):
        assert later > earlier


# --- Rate limiter --------------------------------------------------------------


def test_rate_limiter_waits_for_remaining_interval() -> None:
    clock = ManualClock(1_000)
    sleeps: list[float] = []
    limiter = RateLimiter(min_interval_s=0.5, clock=clock, sleep=sleeps.append)

    limiter.wait()
    assert sleeps == []

    clock.advance(100)
    limiter.wait()
    assert sleeps == [0.4]


def test_rate_limiter_skips_wait_once_interval_elapsed() -> None:
    clock = ManualClock(1_000)
    sleeps: list[float] = []
    limiter = RateLimiter(min_interval_s=0.5, clock=clock, sleep=sleeps.append)

    limiter.wait()
    clock.advance(600)
    limiter.wait()

    assert sleeps == []
