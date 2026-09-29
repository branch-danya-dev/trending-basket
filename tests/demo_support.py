"""Stateful offline Demo exchange; requests cross the real HTTP/signing boundary."""

import copy
import json
from decimal import Decimal as D

import httpx

from trending_basket.clock import ManualClock
from trending_basket.config import Settings
from trending_basket.execution.bybit_private import BybitPrivateClient
from trending_basket.execution.journal import Journal, Notifier
from trending_basket.execution.live_executor import LiveExecutor
from trending_basket.execution.planning import ExchangeRules

SYMBOL = "BTCUSDT"
START = 1735689900000  # 2025-01-01 00:05 UTC
INSTRUMENT = {
    "symbol": SYMBOL,
    "status": "Trading",
    "contractType": "LinearPerpetual",
    "settleCoin": "USDT",
    "fundingInterval": 480,
    "lotSizeFilter": {
        "qtyStep": "0.1",
        "minOrderQty": "0.1",
        "minNotionalValue": "5",
        "maxMktOrderQty": "100",
    },
    "priceFilter": {"tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
}


class DemoAPI:
    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.orders, self.stops, self.positions, self.fills = {}, {}, {}, []
        self.last_price, self.equity, self.liquidation = "100", "1000", "50"
        self.stop_works, self.stop_error = True, 0
        self.timeout_after_fill = False
        self.fill_fraction = D(1)
        self.transactions = []
        self.wallet_override = {}
        self.foreign = False
        self.time_delay_ms = 0
        self.permissions = {
            "readOnly": 0,
            "ips": ["192.0.2.1"],
            "permissions": {"ContractTrade": ["Order", "Position"]},
        }

    @staticmethod
    def response(result, code=0):
        return httpx.Response(
            200, json={"retCode": code, "retMsg": "fixture", "result": copy.deepcopy(result)}
        )

    def put_position(self, quantity, stop="90"):
        self.positions[SYMBOL] = dict(
            symbol=SYMBOL,
            size=str(quantity),
            side="Buy",
            positionIdx=0,
            avgPrice="100",
            markPrice=self.last_price,
            stopLoss=stop,
            liqPrice=self.liquidation,
        )
        if D(stop):
            self.put_stop(SYMBOL, stop)

    def put_stop(self, symbol, stop):
        self.positions[symbol]["stopLoss"] = stop
        self.stops[symbol] = dict(
            symbol=symbol,
            orderId=f"stop-{symbol}",
            orderLinkId="",
            stopOrderType="StopLoss",
            triggerBy="LastPrice",
            triggerPrice=stop,
            qty=self.positions[symbol]["size"],
            reduceOnly=True,
            side="Sell",
            orderStatus="Untriggered",
        )

    def __call__(self, request):
        assert request.url.host == "api-demo.bybit.com"
        params = (
            dict(request.url.params) if request.method == "GET" else json.loads(request.content)
        )
        path = request.url.path
        self.requests.append((request.method, path, params, request))
        if path == "/v5/market/time":
            stamp = self.clock.now_ms()
            self.clock.advance(self.time_delay_ms)
            return self.response({"timeNano": str(stamp * 1000000)})
        if path == "/v5/market/instruments-info":
            return self.response({"list": [INSTRUMENT | {"symbol": params["symbol"]}]})
        if path == "/v5/market/tickers":
            return self.response(
                {"list": [{"symbol": params["symbol"], "lastPrice": self.last_price}]}
            )
        if path == "/v5/account/info":
            return self.response({"marginMode": "REGULAR_MARGIN"})
        if path == "/v5/account/wallet-balance":
            wallet = dict(
                accountType="UNIFIED",
                totalEquity=self.equity,
                totalMarginBalance=self.equity,
                totalAvailableBalance=self.equity,
                totalInitialMargin="0",
                totalMaintenanceMargin="5",
                accountMMRate=str(D(5) / D(self.equity)),
            )
            return self.response({"list": [wallet | self.wallet_override]})
        if path == "/v5/account/transaction-log":
            return self.response({"list": self.transactions, "nextPageCursor": ""})
        if path == "/v5/user/query-api":
            return self.response(self.permissions)
        foreign_market = params.get("category") != "linear" or params.get("settleCoin") == "USDC"
        if path == "/v5/position/list":
            positions = (
                ([{"size": "1"}] if self.foreign else [])
                if foreign_market
                else list(self.positions.values())
            )
            for p in positions:
                if "markPrice" in p:
                    p.setdefault(
                        "unrealisedPnl", str((D(p["markPrice"]) - D(p["avgPrice"])) * D(p["size"]))
                    )
            return self.response({"list": positions, "nextPageCursor": ""})
        if path in {"/v5/order/realtime", "/v5/order/history"}:
            if foreign_market:
                rows = []
            elif params.get("orderId"):
                rows = [
                    o
                    for o in [*self.stops.values(), *self.orders.values()]
                    if o["orderId"] == params["orderId"]
                ]
            elif params.get("orderLinkId"):
                order = self.orders.get(params["orderLinkId"])
                rows = [order] if order else []
            else:
                rows = [
                    *self.stops.values(),
                    *[
                        o
                        for o in self.orders.values()
                        if o["orderStatus"] not in {"Filled", "Cancelled"}
                    ],
                ]
            return self.response({"list": rows, "nextPageCursor": ""})
        if path == "/v5/execution/list":
            rows = [
                f
                for f in self.fills
                if not params.get("orderLinkId") or f["orderLinkId"] == params["orderLinkId"]
            ]
            return self.response({"list": rows, "nextPageCursor": ""})
        if path == "/v5/position/set-leverage":
            return self.response({})
        if path == "/v5/position/trading-stop":
            if self.stop_works:
                self.put_stop(params["symbol"], params["stopLoss"])
            return self.response({}, self.stop_error)
        if path == "/v5/order/create":
            symbol, link = params["symbol"], params["orderLinkId"]
            if link in self.orders:
                return self.response({}, 110072)
            before = D(self.positions.get(symbol, {}).get("size", "0"))
            quantity = D(params["qty"]) * (D(1) if params["reduceOnly"] else self.fill_fraction)
            after = before + (quantity if params["side"] == "Buy" else -quantity)
            assert after >= 0
            order_id = f"order-{len(self.orders)}"
            self.orders[link] = dict(
                params,
                orderId=order_id,
                orderStatus="Filled" if quantity == D(params["qty"]) else "Cancelled",
                cumExecQty=str(quantity),
            )
            self.fills.append(
                dict(
                    symbol=symbol,
                    orderId=order_id,
                    orderLinkId=link,
                    execId=f"fill-{len(self.fills)}",
                    execType="Trade",
                    execTime=str(self.clock.now_ms()),
                    execPrice=self.last_price,
                    execQty=str(quantity),
                    execFee=str(quantity * D(self.last_price) * D("0.00055")),
                    feeCurrency="USDT",
                    side=params["side"],
                )
            )
            if after:
                old_stop = self.positions.get(symbol, {}).get("stopLoss", "0")
                self.positions[symbol] = dict(
                    symbol=symbol,
                    size=str(after),
                    side="Buy",
                    positionIdx=0,
                    avgPrice=self.last_price,
                    markPrice=self.last_price,
                    stopLoss=old_stop,
                    liqPrice=self.liquidation,
                )
                if self.stop_works and (params.get("stopLoss") or D(old_stop)):
                    self.put_stop(symbol, params.get("stopLoss", old_stop))
            else:
                self.positions.pop(symbol, None)
                self.stops.pop(symbol, None)
            if self.timeout_after_fill:
                self.timeout_after_fill = False
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return self.response({"orderId": order_id, "orderLinkId": link})
        if path == "/v5/order/cancel":
            self.orders[params["orderLinkId"]]["orderStatus"] = "Cancelled"
            return self.response({})
        if path == "/v5/order/amend":
            return self.response(params)
        raise AssertionError((path, params))


def setup_demo(tmp_path):
    clock = ManualClock(START)
    settings = Settings(
        _env_file=None,
        mode="demo",
        allocated_capital_usd=1000,
        bybit_api_key="fixture-key",
        bybit_api_secret="fixture-secret",
        data_dir=tmp_path,
    )
    api = DemoAPI(clock)
    journal = Journal(tmp_path / "live" / "demo", clock)
    journal.state["capital"] = dict(
        allocated_usd="1000", start_ms=START - 1000, fills={}, funding={}
    )
    client = BybitPrivateClient(
        settings,
        clock,
        transport=httpx.MockTransport(api),
        sleep=lambda seconds: clock.advance(int(seconds * 1000)),
        on_error=journal.api_error,
    )
    notifier = Notifier(settings, journal)
    notifier.silent = True
    executor = LiveExecutor(client, journal, notifier, {SYMBOL: ExchangeRules.from_api(INSTRUMENT)})
    return settings, clock, api, journal, client, executor


def own_position(journal, api, quantity="2", stop="90"):
    api.put_position(quantity, stop)
    journal.state["positions"][SYMBOL] = dict(
        quantity=quantity,
        average_entry_price=100,
        entry_time_ms=START - 1000,
        lifecycle_id=1,
        stop_price=float(stop),
        initial_risk_usd=20,
        unprotected_since_ms=None,
    )
    journal.state["stop_ids"] = [f"stop-{SYMBOL}"] if D(stop) else []
    journal.save()


def mutations(api, path=None):
    return [r for r in api.requests if r[0] == "POST" and (path is None or r[1] == path)]
