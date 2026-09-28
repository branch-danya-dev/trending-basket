"""Public Bybit REST API v5 client: server time, klines, funding, instruments.

Only public (unauthenticated) endpoints are used; no API keys involved.
Recorded API responses and their provenance live in tests/fixtures/bybit/.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from trending_basket.clock import Clock, SystemClock
from trending_basket.config import Settings
from trending_basket.domain.types import Candle, FundingRate, InstrumentInfo, Interval

_KLINE_PAGE_LIMIT = 1000
_FUNDING_PAGE_LIMIT = 200
_INSTRUMENTS_PAGE_LIMIT = 1000
_INSTRUMENT_STATUSES = ("Trading", "Closed", "PreLaunch", "PendingOpen", "Delivering")

_RATE_LIMIT_RET_CODE = 10006
_RETRYABLE_RET_CODES = frozenset({_RATE_LIMIT_RET_CODE})
_NON_RET_CODE = -1


class BybitAPIError(RuntimeError):
    """A Bybit response with a non-zero retCode, or exhausted retries.

    For a transport-level failure (timeout, connection error, HTTP 5xx/429)
    there is no real Bybit retCode, so `ret_code` is set to -1 and `ret_msg`
    carries a description of what actually happened.
    """

    def __init__(self, ret_code: int, ret_msg: str, endpoint: str, params: dict[str, Any]) -> None:
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint
        self.params = params
        super().__init__(f"{endpoint}: retCode={ret_code} retMsg={ret_msg!r} params={params}")


@dataclass
class RateLimiter:
    """Enforces a minimum gap between requests, using an injected clock and sleep."""

    min_interval_s: float
    clock: Clock
    sleep: Callable[[float], None]
    _last_request_ms: int | None = field(default=None, init=False, repr=False)

    def wait(self) -> None:
        if self._last_request_ms is not None:
            elapsed_s = (self.clock.now_ms() - self._last_request_ms) / 1000
            remaining_s = self.min_interval_s - elapsed_s
            if remaining_s > 0:
                self.sleep(remaining_s)
        self._last_request_ms = self.clock.now_ms()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter for retryable request failures."""

    max_retries: int
    backoff_base_s: float = 0.5
    jitter_frac: float = 0.25
    random_fn: Callable[[], float] = random.random

    def backoff_s(self, attempt: int) -> float:
        base_s: float = self.backoff_base_s * (2**attempt)
        jitter_s: float = base_s * self.jitter_frac * self.random_fn()
        result: float = base_s + jitter_s
        return result


class BybitPublicClient:
    """Client for Bybit's public (unauthenticated) v5 REST API, category=linear."""

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        clock: Clock,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        sleep: Callable[[float], None],
        kline_page_limit: int = _KLINE_PAGE_LIMIT,
        funding_page_limit: int = _FUNDING_PAGE_LIMIT,
    ) -> None:
        self._http = http_client
        self._clock = clock
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._kline_page_limit = kline_page_limit
        self._funding_page_limit = funding_page_limit

    def server_time_ms(self) -> int:
        payload = self._request("/v5/market/time", {})
        return int(payload["result"]["timeSecond"]) * 1000

    def fetch_klines(
        self, symbol: str, interval: Interval, start_ms: int, end_ms: int
    ) -> list[Candle]:
        """Fetch one page of klines, ascending, unclosed candle dropped."""
        rows = self._fetch_kline_rows(symbol, interval, start_ms, end_ms)
        return self._parse_closed_klines(symbol, interval, rows)

    def _fetch_kline_rows(
        self, symbol: str, interval: Interval, start_ms: int, end_ms: int
    ) -> list[list[str]]:
        payload = self._request(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval.to_bybit(),
                "start": start_ms,
                "end": end_ms,
                "limit": self._kline_page_limit,
            },
        )
        rows: list[list[str]] = payload["result"]["list"]
        return rows

    def _parse_closed_klines(
        self, symbol: str, interval: Interval, rows: list[list[str]]
    ) -> list[Candle]:
        server_now_ms = self.server_time_ms()
        candles = []
        for row in rows:
            open_time_ms = int(row[0])
            if open_time_ms + interval.duration_ms > server_now_ms:
                continue
            candles.append(
                Candle(
                    symbol=symbol,
                    interval=interval,
                    open_time_ms=open_time_ms,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[6]),
                )
            )
        candles.sort(key=lambda c: c.open_time_ms)
        return candles

    def iter_klines(
        self, symbol: str, interval: Interval, start_ms: int, end_ms: int
    ) -> Iterator[Candle]:
        """Paginate klines across [start_ms, end_ms], ascending, no dupes/gaps at page joins."""
        by_open_time: dict[int, Candle] = {}
        page_end_ms = end_ms

        while True:
            rows = self._fetch_kline_rows(symbol, interval, start_ms, page_end_ms)
            if not rows:
                break
            page = self._parse_closed_klines(symbol, interval, rows)
            for candle in page:
                by_open_time[candle.open_time_ms] = candle

            # A full page can become shorter (or empty) after dropping unclosed candles.
            earliest_ms = min(int(row[0]) for row in rows)
            if earliest_ms <= start_ms or len(rows) < self._kline_page_limit:
                break
            page_end_ms = earliest_ms - 1

        for open_time_ms in sorted(by_open_time):
            yield by_open_time[open_time_ms]

    def fetch_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[FundingRate]:
        """Fetch the full funding rate history in [start_ms, end_ms], ascending, deduped."""
        by_time: dict[int, FundingRate] = {}
        page_end_ms = end_ms

        while True:
            payload = self._request(
                "/v5/market/funding/history",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": start_ms,
                    "endTime": page_end_ms,
                    "limit": self._funding_page_limit,
                },
            )
            rows = payload["result"]["list"]
            if not rows:
                break
            for row in rows:
                funding_time_ms = int(row["fundingRateTimestamp"])
                by_time[funding_time_ms] = FundingRate(
                    symbol=symbol,
                    funding_time_ms=funding_time_ms,
                    rate_frac=float(row["fundingRate"]),
                )

            earliest_ms = min(int(row["fundingRateTimestamp"]) for row in rows)
            if earliest_ms <= start_ms or len(rows) < self._funding_page_limit:
                break
            page_end_ms = earliest_ms - 1

        return [by_time[t] for t in sorted(by_time)]

    def fetch_instruments(self) -> list[InstrumentInfo]:
        """Query every documented status, retaining actual response statuses and duplicates once."""
        instruments: dict[str, InstrumentInfo] = {}
        for status in _INSTRUMENT_STATUSES:
            for instrument in self._fetch_instrument_status(status):
                previous = instruments.get(instrument.symbol)
                if previous is not None and previous != instrument:
                    raise BybitAPIError(
                        _NON_RET_CODE,
                        f"conflicting instrument rows for {instrument.symbol}; retry snapshot",
                        "/v5/market/instruments-info",
                        {"category": "linear", "status": status},
                    )
                instruments[instrument.symbol] = instrument
        return [instruments[symbol] for symbol in sorted(instruments)]

    def _fetch_instrument_status(self, status: str) -> Iterator[InstrumentInfo]:
        cursor = ""
        seen_cursors: set[str] = set()

        while True:
            params: dict[str, Any] = {
                "category": "linear",
                "limit": _INSTRUMENTS_PAGE_LIMIT,
                "status": status,
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._request("/v5/market/instruments-info", params)
            result = payload["result"]

            for row in result["list"]:
                price_filter = row["priceFilter"]
                lot_size_filter = row["lotSizeFilter"]
                yield InstrumentInfo(
                    symbol=row["symbol"],
                    contract_type=row["contractType"],
                    status=row["status"],
                    base_coin=row["baseCoin"],
                    quote_coin=row["quoteCoin"],
                    launch_time_ms=int(row["launchTime"]),
                    delivery_time_ms=int(row.get("deliveryTime") or 0),
                    funding_interval_ms=int(row["fundingInterval"]) * 60_000,
                    tick_size=Decimal(price_filter["tickSize"]),
                    min_order_qty=Decimal(lot_size_filter["minOrderQty"]),
                    max_order_qty=Decimal(lot_size_filter["maxOrderQty"]),
                    qty_step=Decimal(lot_size_filter["qtyStep"]),
                    min_notional_value=(
                        Decimal(lot_size_filter["minNotionalValue"])
                        if lot_size_filter.get("minNotionalValue")
                        else None
                    ),
                    symbol_type=row.get("symbolType"),
                    market_region=row.get("marketRegion"),
                    underlying_ticker=row.get("underlyingTicker"),
                )

            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise BybitAPIError(
                    _NON_RET_CODE,
                    "repeated instrument cursor",
                    "/v5/market/instruments-info",
                    params,
                )
            seen_cursors.add(cursor)

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        last_ret_code = _NON_RET_CODE
        last_ret_msg = "no attempts made"
        max_retries = self._retry_policy.max_retries

        for attempt in range(max_retries + 1):
            self._rate_limiter.wait()

            try:
                response = self._http.get(endpoint, params=params)
            except httpx.TransportError as exc:
                last_ret_code, last_ret_msg = _NON_RET_CODE, f"{type(exc).__name__}: {exc}"
                if attempt < max_retries:
                    self._sleep(self._retry_policy.backoff_s(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_ret_code = _NON_RET_CODE
                last_ret_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                if attempt < max_retries:
                    self._sleep(self._retry_policy.backoff_s(attempt))
                continue

            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            ret_code = int(payload["retCode"])
            if ret_code == 0:
                return payload

            last_ret_code = ret_code
            last_ret_msg = str(payload.get("retMsg", ""))
            if ret_code in _RETRYABLE_RET_CODES:
                if attempt < max_retries:
                    self._sleep(self._retry_policy.backoff_s(attempt))
                continue

            raise BybitAPIError(ret_code, last_ret_msg, endpoint, params)

        raise BybitAPIError(last_ret_code, last_ret_msg, endpoint, params)


def build_client(
    settings: Settings,
    *,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> BybitPublicClient:
    """Build a production-configured client. Tests override `transport` with a MockTransport."""
    resolved_clock = clock or SystemClock()
    resolved_sleep = sleep or time.sleep
    http_client = httpx.Client(
        base_url=settings.bybit_rest_url,
        timeout=settings.rest_timeout_s,
        transport=transport,
    )
    rate_limiter = RateLimiter(
        min_interval_s=settings.rest_min_interval_s, clock=resolved_clock, sleep=resolved_sleep
    )
    retry_policy = RetryPolicy(max_retries=settings.rest_max_retries)
    return BybitPublicClient(
        http_client,
        clock=resolved_clock,
        rate_limiter=rate_limiter,
        retry_policy=retry_policy,
        sleep=resolved_sleep,
    )
