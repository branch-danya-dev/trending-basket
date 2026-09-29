"""Signed Bybit v5 requests restricted to Demo, with recoverable order IDs."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from trending_basket.clock import Clock
from trending_basket.config import Settings

DEMO_URL = "https://api-demo.bybit.com"


class PrivateAPIError(RuntimeError):
    def __init__(self, path: str, code: int, *, ambiguous: bool = False) -> None:
        self.code, self.ambiguous = code, ambiguous
        # Do not expose response text, URLs, credentials or request headers.
        super().__init__(f"Bybit {path}: error {code}; ambiguous={ambiguous}")


def signature(secret: str, timestamp_ms: int, key: str, window_ms: int, body: str) -> str:
    message = f"{timestamp_ms}{key}{window_ms}{body}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class BybitPrivateClient:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_error: Callable[[], None] | None = None,
    ) -> None:
        if settings.mode != "demo" or settings.bybit_demo_rest_url != DEMO_URL:
            raise ValueError("private execution requires demo and exact Demo URL")
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise ValueError("Bybit Demo credentials are missing")
        self.key = settings.bybit_api_key.get_secret_value()
        self.secret = settings.bybit_api_secret.get_secret_value()
        self.clock, self.sleep, self.on_error = clock, sleep, on_error
        self.http = httpx.Client(
            base_url=DEMO_URL,
            timeout=min(settings.rest_timeout_s, 5),
            follow_redirects=False,
            transport=transport,
        )
        self.read_only = False
        self.offset_ms = 0
        self.synced_at_ms: int | None = None
        self.recv_window_ms = 5000

    def close(self) -> None:
        self.http.close()

    def now_ms(self) -> int:
        return self.clock.now_ms() + self.offset_ms

    def _error(self, path: str, code: int, ambiguous: bool = False) -> PrivateAPIError:
        if self.on_error:
            self.on_error()
        return PrivateAPIError(path, code, ambiguous=ambiguous)

    def sync_time(self) -> None:
        before = self.clock.now_ms()
        result = self.request("GET", "/v5/market/time", {}, signed=False)
        after = self.clock.now_ms()
        server_ms = int(result["timeNano"]) // 1_000_000
        if after - before > 1000:
            self.synced_at_ms = None
            raise self._error("/v5/market/time", 10002)
        self.offset_ms = server_ms - (before + after) // 2
        self.synced_at_ms = after

    def request(
        self, method: str, path: str, params: dict[str, Any], *, signed: bool = True
    ) -> dict[str, Any]:
        if self.read_only and method != "GET":
            raise ValueError("read-only Demo operation forbids exchange mutations")
        if not path.startswith("/v5/") or "?" in path or ":" in path:
            raise ValueError("invalid API path")
        if signed and (
            self.synced_at_ms is None or not 0 <= self.clock.now_ms() - self.synced_at_ms <= 60_000
        ):
            self.sync_time()
        body = (
            urlencode(sorted(params.items()))
            if method == "GET"
            else json.dumps(params, separators=(",", ":"), sort_keys=True)
        )
        headers = {"Content-Type": "application/json"}
        if signed:
            stamp = self.now_ms()
            headers.update(
                {
                    "X-BAPI-API-KEY": self.key,
                    "X-BAPI-TIMESTAMP": str(stamp),
                    "X-BAPI-RECV-WINDOW": str(self.recv_window_ms),
                    "X-BAPI-SIGN": signature(
                        self.secret, stamp, self.key, self.recv_window_ms, body
                    ),
                }
            )
        try:
            response = self.http.request(
                method,
                path + ("?" + body if method == "GET" and body else ""),
                content=body if method != "GET" else None,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise self._error(path, -1, ambiguous=method != "GET") from None
        code = int(payload.get("retCode", -1))
        if (code == 110043 and path == "/v5/position/set-leverage") or (
            code == 34040 and path == "/v5/position/trading-stop"
        ):
            return {}
        if code:
            if code == 10002:
                self.synced_at_ms = None
            raise self._error(path, code, ambiguous=code in {10000, 10006, 10016, 10014, 110072})
        return dict(payload["result"])

    def pages(self, path: str, **params: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            result = self.request("GET", path, params)
            rows.extend(result["list"])
            cursor = result.get("nextPageCursor", "")
            if not cursor:
                return rows
            if cursor in seen:
                raise ValueError("repeated API pagination cursor")
            seen.add(cursor)
            params["cursor"] = cursor

    def account(self) -> dict[str, Any]:
        return self.request("GET", "/v5/account/info", {})

    def wallet(self) -> dict[str, Any]:
        return dict(
            self.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})["list"][0]
        )

    def key_permissions(self) -> dict[str, Any]:
        result = self.request("GET", "/v5/user/query-api", {})
        return {key: result.get(key) for key in ("readOnly", "permissions", "ips")}

    def positions(self) -> list[dict[str, Any]]:
        return self.pages("/v5/position/list", category="linear", settleCoin="USDT", limit=200)

    def open_orders(self) -> list[dict[str, Any]]:
        return self.pages(
            "/v5/order/realtime", category="linear", settleCoin="USDT", openOnly=0, limit=50
        )

    def foreign_markets(self) -> bool:
        for category, extra in (
            ("linear", {"settleCoin": "USDC"}),
            ("inverse", {}),
            ("option", {}),
        ):
            if any(
                Decimal(p["size"]) != 0
                for p in self.pages("/v5/position/list", category=category, **extra)
            ):
                return True
        for category, extra in (
            ("linear", {"settleCoin": "USDC"}),
            ("inverse", {}),
            ("option", {}),
            ("spot", {}),
        ):
            if self.pages("/v5/order/realtime", category=category, openOnly=0, **extra):
                return True
        return False

    def executions(self, **filters: Any) -> list[dict[str, Any]]:
        return self.pages("/v5/execution/list", category="linear", limit=100, **filters)

    def find_order(self, link_id: str, symbol: str) -> dict[str, Any] | None:
        for path in ("/v5/order/realtime", "/v5/order/history"):
            rows = self.pages(path, category="linear", symbol=symbol, orderLinkId=link_id, limit=50)
            matching = [row for row in rows if row.get("orderLinkId") == link_id]
            if matching:
                return matching[0]
        return None

    def order_by_id(self, symbol: str, order_id: str) -> dict[str, Any] | None:
        rows = self.pages(
            "/v5/order/history", category="linear", symbol=symbol, orderId=order_id, limit=50
        )
        return next((row for row in rows if row.get("orderId") == order_id), None)

    def create_order(
        self, params: dict[str, Any], *, allow_submit: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        link, symbol = params["orderLinkId"], params["symbol"]
        existing = self.find_order(link, symbol)
        if existing is not None:
            return existing
        for attempt in range(3):
            try:
                if allow_submit:
                    allow_submit()
                return self.request("POST", "/v5/order/create", params)
            except PrivateAPIError as exc:
                if not exc.ambiguous:
                    raise
                # Acknowledgement may have been lost after acceptance. Never change the ID.
                self.sleep(1)
                existing = self.find_order(link, symbol)
                if existing is not None:
                    return existing
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def amend_order(self, symbol: str, link_id: str, **changes: Any) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v5/order/amend",
            dict(category="linear", symbol=symbol, orderLinkId=link_id, **changes),
        )

    def cancel_order(self, symbol: str, link_id: str) -> None:
        self.request(
            "POST", "/v5/order/cancel", dict(category="linear", symbol=symbol, orderLinkId=link_id)
        )

    def set_stop(self, symbol: str, price: str) -> None:
        self.request(
            "POST",
            "/v5/position/trading-stop",
            dict(
                category="linear",
                symbol=symbol,
                positionIdx=0,
                tpslMode="Full",
                stopLoss=price,
                slTriggerBy="LastPrice",
                slOrderType="Market",
            ),
        )

    def set_leverage(self, symbol: str, leverage: str = "3") -> None:
        try:
            self.request(
                "POST",
                "/v5/position/set-leverage",
                dict(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage),
            )
        except PrivateAPIError as exc:
            if exc.code != 110043:
                raise

    def instrument(self, symbol: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                "/v5/market/instruments-info",
                dict(category="linear", symbol=symbol),
                signed=False,
            )["list"][0]
        )

    def ticker(self, symbol: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET", "/v5/market/tickers", dict(category="linear", symbol=symbol), signed=False
            )["list"][0]
        )
