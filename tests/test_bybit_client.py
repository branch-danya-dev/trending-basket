"""Tests for BybitPublicClient: parsing, pagination, retries, rate limiting."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from itertools import pairwise

import httpx
import pytest
from bybit_test_support import load_fixture, route_transport

from trending_basket.clock import ManualClock
from trending_basket.data import bybit_client
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

    candles = client.fetch_klines("BTCUSDT", Interval.D1, 1704067200000, 1704499199999)

    assert [c.open_time_ms for c in candles] == [1704240000000, 1704326400000, 1704412800000]
    assert candles[0].close == 42871.9


def test_fetch_klines_drops_unclosed_last_candle() -> None:
    transport = route_transport(
        {
            "/v5/market/time": [load_fixture("server_time_for_unclosed_4h.json")],
            "/v5/market/kline": [load_fixture("kline_4h_with_unclosed_last.json")],
        }
    )
    client, _, _ = _make_client(transport)

    candles = client.fetch_klines("BTCUSDT", Interval.H4, 1790568000000, 1790634506000)

    assert [c.open_time_ms for c in candles] == [1790596800000, 1790611200000]


# --- Pagination --------------------------------------------------------------


def test_iter_klines_paginates_without_dupes_or_gaps() -> None:
    requests: list[httpx.Request] = []
    transport = route_transport(
        {
            "/v5/market/time": [load_fixture("server_time.json")],
            "/v5/market/kline": [
                load_fixture("kline_1d_page1_recent.json"),
                load_fixture("kline_1d_page2_older.json"),
            ],
        },
        requests=requests,
    )
    client, _, _ = _make_client(transport, kline_page_limit=3)

    candles = list(client.iter_klines("BTCUSDT", Interval.D1, 1704067200000, 1704499199999))

    assert [c.open_time_ms for c in candles] == [
        1704067200000,
        1704153600000,
        1704240000000,
        1704326400000,
        1704412800000,
    ]
    assert len(candles) == len({c.open_time_ms for c in candles})
    kline_requests = [r for r in requests if r.url.path == "/v5/market/kline"]
    assert [dict(r.url.params) for r in kline_requests] == [
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": "D",
            "start": "1704067200000",
            "end": str(end),
            "limit": "3",
        }
        for end in (1704499199999, 1704239999999)
    ]


@pytest.mark.parametrize("page_limit", [1, 3])
def test_iter_klines_continues_after_dropping_unclosed_candle(page_limit: int) -> None:
    recent = load_fixture("kline_4h_with_unclosed_last.json")
    older = load_fixture("kline_4h_page2_older.json")
    rows = recent["result"]["list"] + older["result"]["list"]
    # Re-page the recorded rows to also exercise an all-unclosed first page.
    pages = []
    for offset in range(0, len(rows), page_limit):
        page = deepcopy(recent)
        page["result"]["list"] = rows[offset : offset + page_limit]
        pages.append(page)
    requests: list[httpx.Request] = []
    transport = route_transport(
        {
            "/v5/market/time": [load_fixture("server_time_for_unclosed_4h.json")],
            "/v5/market/kline": pages,
        },
        requests=requests,
    )
    client, _, _ = _make_client(transport, kline_page_limit=page_limit)

    candles = list(client.iter_klines("BTCUSDT", Interval.H4, 1790568000000, 1790634506000))

    assert [c.open_time_ms for c in candles] == [
        1790568000000,
        1790582400000,
        1790596800000,
        1790611200000,
    ]
    kline_requests = [r for r in requests if r.url.path == "/v5/market/kline"]
    assert len(kline_requests) == len(pages)
    for previous, request in zip(pages, kline_requests[1:], strict=False):
        assert int(request.url.params["end"]) == int(previous["result"]["list"][-1][0]) - 1


def test_iter_klines_stops_on_empty_response() -> None:
    empty = load_fixture("kline_1d_page1_recent.json")
    empty["result"]["list"] = []
    requests: list[httpx.Request] = []
    transport = route_transport({"/v5/market/kline": [empty]}, requests=requests)
    client, _, _ = _make_client(transport)

    assert list(client.iter_klines("BTCUSDT", Interval.D1, 0, 1)) == []
    assert len(requests) == 1


def test_fetch_funding_history_paginates_backward() -> None:
    requests: list[httpx.Request] = []
    transport = route_transport(
        {
            "/v5/market/funding/history": [
                load_fixture("funding_page1_recent.json"),
                load_fixture("funding_page2_older.json"),
            ],
        },
        requests=requests,
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
    assert [dict(r.url.params) for r in requests] == [
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "startTime": "1704067200000",
            "endTime": str(end),
            "limit": "3",
        }
        for end in (1704182400000, 1704124799999)
    ]


def test_fetch_instruments_paginates_via_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bybit_client, "_INSTRUMENTS_PAGE_LIMIT", 500)
    requests: list[httpx.Request] = []
    transport = route_transport(
        {
            "/v5/market/instruments-info": [
                load_fixture("instruments_page1_cursor.json"),
                load_fixture("instruments_page2_final.json"),
            ],
        },
        requests=requests,
    )
    client, _, _ = _make_client(transport)

    instruments = client.fetch_instruments()

    assert [i.symbol for i in instruments] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert instruments[0].tick_size == Decimal("0.10")
    assert instruments[0].min_order_qty == Decimal("0.001")
    assert instruments[0].funding_interval_ms == 480 * 60_000
    assert instruments[0].min_notional_value == Decimal("5")
    assert [dict(r.url.params) for r in requests] == [
        {"category": "linear", "limit": "500"},
        {
            "category": "linear",
            "limit": "500",
            "cursor": "first%3D0GUSDT%26last%3DMONUSDT",
        },
    ]


# --- Retries -----------------------------------------------------------------


def test_retries_on_http_429_then_succeeds() -> None:
    transport = route_transport({"/v5/market/time": [(429, {}), load_fixture("server_time.json")]})
    client, _, sleeps = _make_client(transport, max_retries=2)

    assert client.server_time_ms() == 1790634505000
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

    assert client.server_time_ms() == 1790634505000
    assert len(sleeps) == 1


def test_non_retryable_ret_code_raises_immediately() -> None:
    transport = route_transport(
        {"/v5/market/funding/history": [load_fixture("non_retryable_error.json")]}
    )
    client, _, sleeps = _make_client(transport, max_retries=5)

    with pytest.raises(BybitAPIError) as exc_info:
        client.fetch_funding_history("BTCUSDT", 1704067200000, 1704182400000)

    assert exc_info.value.ret_code == 10001
    assert exc_info.value.endpoint == "/v5/market/funding/history"
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
