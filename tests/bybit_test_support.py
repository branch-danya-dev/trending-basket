"""Shared test helpers for building fake Bybit responses. Not a test module itself."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from trending_basket.data.bybit_client import BybitAPIError
from trending_basket.domain.types import Candle, FundingRate, InstrumentInfo, Interval

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "bybit"

RouteItem = dict[str, Any] | tuple[int, dict[str, Any]]


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def route_transport(routes: dict[str, list[RouteItem]]) -> httpx.MockTransport:
    """MockTransport routing by URL path.

    A route with exactly one queued item repeats it forever (handy for
    /v5/market/time, which does not change mid-test). A route with more than
    one item is consumed in order; requesting past the end is an error.
    """
    queues = {path: list(items) for path, items in routes.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        queue = queues.get(path)
        if not queue:
            raise AssertionError(f"no mock response queued for {path}")
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(item, tuple):
            status, body = item
            return httpx.Response(status, json=body)
        return httpx.Response(200, json=item)

    return httpx.MockTransport(handler)


def make_candle(symbol: str, open_time_ms: int, interval: Interval = Interval.D1) -> Candle:
    """A simple valid Candle at a given aligned open time, for cache/CLI tests."""
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time_ms=open_time_ms,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10.0,
        turnover=1000.0,
    )


@dataclass
class FakeBybitClient:
    """Minimal stand-in for BybitPublicClient, for cache- and CLI-layer tests.

    Holds the full universe of data "available on the exchange"; each fetch
    method filters it to the requested range, like the real API would.
    Symbols in `fail_symbols` raise BybitAPIError instead, to simulate a
    per-symbol failure.
    """

    server_now_ms: int
    all_candles: list[Candle] = field(default_factory=list)
    all_funding: list[FundingRate] = field(default_factory=list)
    all_instruments: list[InstrumentInfo] = field(default_factory=list)
    fail_symbols: frozenset[str] = frozenset()
    range_calls: list[tuple[int, int]] = field(default_factory=list)

    def server_time_ms(self) -> int:
        return self.server_now_ms

    def iter_klines(
        self, symbol: str, interval: Interval, start_ms: int, end_ms: int
    ) -> Iterator[Candle]:
        if symbol in self.fail_symbols:
            raise BybitAPIError(-1, "simulated failure", "/v5/market/kline", {"symbol": symbol})
        self.range_calls.append((start_ms, end_ms))
        return iter(
            c
            for c in self.all_candles
            if c.symbol == symbol
            and c.interval == interval
            and start_ms <= c.open_time_ms <= end_ms
        )

    def fetch_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[FundingRate]:
        if symbol in self.fail_symbols:
            raise BybitAPIError(
                -1, "simulated failure", "/v5/market/funding-history", {"symbol": symbol}
            )
        self.range_calls.append((start_ms, end_ms))
        return [
            r
            for r in self.all_funding
            if r.symbol == symbol and start_ms <= r.funding_time_ms <= end_ms
        ]

    def fetch_instruments(self) -> list[InstrumentInfo]:
        return list(self.all_instruments)
