"""Execution acceptance criteria on an offline, stateful HTTP exchange."""

import copy
import json
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from demo_support import INSTRUMENT, START, SYMBOL, mutations, own_position, setup_demo
from pydantic import SecretStr
from typer.testing import CliRunner

from trending_basket.cli import app
from trending_basket.clock import ManualClock
from trending_basket.config import Settings, format_settings
from trending_basket.execution.bybit_private import (
    DEMO_URL,
    BybitPrivateClient,
    PrivateAPIError,
    signature,
)
from trending_basket.execution.cli import resume_checkpoint
from trending_basket.execution.cycle import check_account, decision_day, drawdown_guard
from trending_basket.execution.journal import Halted, Journal, Notifier, PreviewJournal
from trending_basket.execution.planning import ExchangeRules, planned_delta
from trending_basket.portfolio.limits import PortfolioLimits, limit_violations
from trending_basket.strategies.base import TargetPosition


@pytest.mark.parametrize(
    "mode,url",
    [
        ("live", DEMO_URL),
        ("research", DEMO_URL),
        ("paper", DEMO_URL),
        ("demo", "https://api.bybit.com"),
        ("demo", "https://api-testnet.bybit.com"),
        ("demo", DEMO_URL + "/"),
        ("demo", DEMO_URL + ".evil.invalid"),
    ],
)
def test_private_client_rejects_every_non_demo_configuration(mode, url):
    settings = Settings(
        _env_file=None,
        mode=mode,
        bybit_demo_rest_url=url,
        bybit_api_key="key",
        bybit_api_secret="secret",
    )
    with pytest.raises(ValueError, match="requires demo"):
        BybitPrivateClient(settings, ManualClock(START))


def test_hmac_known_literal_and_wire_body(tmp_path):
    # Independent .NET HMACSHA256 oracle for the literal v5 concatenation:
    # 1658385579423fixture-key5000category=linear&symbol=BTCUSDT
    assert (
        signature(
            "fixture-secret", 1658385579423, "fixture-key", 5000, "category=linear&symbol=BTCUSDT"
        )
        == "7b79b0847db644da4d5b189136381b3a79bb6531d8a0489ee9d7e63959e33e21"
    )
    _, _, api, _, client, _ = setup_demo(tmp_path)
    client.wallet()
    client.set_leverage(SYMBOL)
    for method, _, _, request in api.requests:
        if "X-BAPI-SIGN" not in request.headers:
            continue
        signed_body = request.url.query.decode() if method == "GET" else request.content.decode()
        assert request.headers["X-BAPI-SIGN"] == signature(
            "fixture-secret",
            int(request.headers["X-BAPI-TIMESTAMP"]),
            "fixture-key",
            5000,
            signed_body,
        )


def test_timeout_after_acceptance_recovers_same_id_and_actual_fees(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    api.timeout_after_fill = True
    executor.rebalance(SYMBOL, TargetPosition(200, 90, 20), 100, 100, START)
    assert len(mutations(api, "/v5/order/create")) == 1
    assert D(api.positions[SYMBOL]["size"]) == D("1.9")  # floor(200/100.02 / .1)
    assert journal.state["positions"][SYMBOL]["quantity"] == "1.9"
    assert journal.state["pending_order"] is None
    assert D(api.positions[SYMBOL]["stopLoss"]) == 90
    assert journal.state["stop_ids"] == ["stop-BTCUSDT"]
    fill = json.loads((journal.directory / "fills.jsonl").read_text().splitlines()[0])
    assert fill["fee_usd"] == pytest.approx(0.1045)
    assert fill["decision_price"] == 100 and fill["execution_price"] == "100"
    assert len(fill["order_link_id"]) <= 36


def test_partial_ioc_does_not_invent_unfilled_quantity(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    api.fill_fraction = D("0.5")
    executor.rebalance(SYMBOL, TargetPosition(210, 90), 100, 100, START)
    assert D(journal.state["positions"][SYMBOL]["quantity"]) == 1
    assert len(mutations(api, "/v5/order/create")) == 1
    assert D(api.stops[SYMBOL]["qty"]) == 1


def test_target_delta_reads_exchange_and_minimum_reduction_overshoots_safely(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api, "2")
    executor.rules[SYMBOL] = ExchangeRules.from_api(
        copy.deepcopy(INSTRUMENT)
        | {"lotSizeFilter": INSTRUMENT["lotSizeFilter"] | {"minOrderQty": "0.5"}}
    )
    # Desired=1.9, reduction .1 is not executable; reduce .5, actual=1.5 <= target.
    executor.rebalance(SYMBOL, TargetPosition(190, 90), 100, 100, START)
    create = mutations(api, "/v5/order/create")[0][2]
    assert create["side"] == "Sell" and create["reduceOnly"] is True
    assert D(create["qty"]) == D("0.5") and D(api.positions[SYMBOL]["size"]) == D("1.5")
    assert D(api.stops[SYMBOL]["qty"]) == D("1.5")


@pytest.mark.parametrize("error", [0, 10016])
def test_stop_timeout_closes_even_after_five_api_errors(tmp_path, error):
    _, clock, api, journal, _, executor = setup_demo(tmp_path)
    api.stop_works = False
    api.stop_error = error
    with pytest.raises(Halted, match="stop unconfirmed"):
        executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    assert api.positions == {} and journal.state["positions"] == {}
    assert clock.now_ms() - START >= 60000
    orders = mutations(api, "/v5/order/create")
    assert len(orders) == 2 and orders[-1][2]["reduceOnly"] is True
    assert journal.state["halt"]


def test_trailing_stop_never_loosens_and_rounds_toward_protection(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api)
    executor.confirm_stop(SYMBOL, 85)
    assert api.positions[SYMBOL]["stopLoss"] == "90"
    executor.confirm_stop(SYMBOL, 91.01)
    assert api.positions[SYMBOL]["stopLoss"] == "91.1"


def test_position_read_alone_does_not_confirm_partial_or_wrong_trigger_stop(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api)
    api.stop_works = False
    api.stops[SYMBOL]["triggerBy"] = "MarkPrice"
    with pytest.raises(Halted, match="stop unconfirmed"):
        executor.confirm_stop(SYMBOL, 90)
    assert not api.positions


def test_gap_beyond_decision_stop_never_enters(tmp_path):
    _, _, api, _, _, executor = setup_demo(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 101), 100, 100, START)
    assert not mutations(api)


@pytest.mark.parametrize("liq", ["95", ""])
def test_liquidation_distance_reduces_until_safe_or_flat(tmp_path, liq):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    api.liquidation = liq
    own_position(journal, api)
    executor.ensure_liquidation_distance(SYMBOL, 100, START)
    assert not api.positions
    orders = mutations(api, "/v5/order/create")
    assert all(o[2]["reduceOnly"] for o in orders)


def test_actual_equity_loss_minimum_orders_still_enforce_limits(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api, "2")
    api.equity = "390"  # 200/390 > 0.5, even though before loss 200/400 == 0.5.
    executor.rules[SYMBOL] = ExchangeRules.from_api(
        copy.deepcopy(INSTRUMENT)
        | {"lotSizeFilter": INSTRUMENT["lotSizeFilter"] | {"minOrderQty": "0.5"}}
    )
    limits = PortfolioLimits(max_gross_exposure=2, max_net_exposure=1.5, max_symbol_exposure=0.5)
    executor.enforce_limits({SYMBOL: 100}, START, limits)
    actual = {s: float(p["size"]) * 100 for s, p in api.positions.items()}
    assert not limit_violations(actual, 390, limits)
    assert "actual_limit_reduction" in (journal.directory / "orders.jsonl").read_text()


@pytest.mark.parametrize("foreign", ["position", "short", "order", "other_market"])
def test_foreign_or_mismatched_account_halts_without_any_mutation(tmp_path, foreign):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    if foreign in {"position", "short"}:
        api.put_position("1")
        if foreign == "short":
            api.positions[SYMBOL]["side"] = "Sell"
    elif foreign == "order":
        api.orders["not-bot"] = {
            "symbol": SYMBOL,
            "orderId": "foreign",
            "orderLinkId": "not-bot",
            "orderStatus": "New",
        }
    else:
        api.foreign = True
    with pytest.raises(Halted):
        executor.reconcile()
    assert mutations(api) == []
    if foreign != "short":
        assert journal.state["halt"]


def test_read_only_transport_blocks_every_mutation(tmp_path):
    _, _, api, _, client, _ = setup_demo(tmp_path)
    client.read_only = True
    for action in (
        lambda: client.set_leverage(SYMBOL),
        lambda: client.set_stop(SYMBOL, "90"),
        lambda: client.cancel_order(SYMBOL, "x"),
        lambda: client.amend_order(SYMBOL, "x", qty="1"),
    ):
        with pytest.raises(ValueError, match="read-only"):
            action()
    assert not mutations(api)


def test_preflight_auth_rights_foreign_checks_are_get_only(tmp_path):
    _, _, api, _, client, executor = setup_demo(tmp_path)
    result = check_account(client, executor)
    assert result["equity_usd"] == 1000 and result["key_permissions_checked"]
    assert not mutations(api)
    api.permissions["permissions"]["Wallet"] = ["Withdraw"]
    with pytest.raises(ValueError, match="withdrawal"):
        check_account(client, executor)
    assert not mutations(api)


def test_unsynchronized_time_prevents_signed_request(tmp_path):
    _, _, api, journal, client, _ = setup_demo(tmp_path)
    api.time_delay_ms = 1001
    with pytest.raises(PrivateAPIError) as caught:
        client.set_leverage(SYMBOL)
    assert caught.value.code == 10002
    assert not mutations(api) and client.synced_at_ms is None
    assert journal.state["api_errors"] == 1


def test_five_errors_persist_and_block_entry(tmp_path):
    _, clock, api, journal, _, executor = setup_demo(tmp_path)
    for _ in range(5):
        journal.api_error()
    reopened = Journal(journal.directory, clock)
    assert reopened.state["halt"] and reopened.state["api_errors"] == 5
    with pytest.raises(Halted):
        executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    assert not mutations(api)


def test_drawdown_exact_threshold_allowed_excess_stops(tmp_path):
    _, _, _, journal, _, _ = setup_demo(tmp_path)
    drawdown_guard(journal, 1000, 0.2)
    drawdown_guard(journal, 800, 0.2)
    with pytest.raises(Halted, match="drawdown"):
        drawdown_guard(journal, 799.99, 0.2)


def test_resume_requires_confirmation_and_reconciliation(tmp_path, monkeypatch):
    settings, _, api, journal, _, executor = setup_demo(tmp_path)
    journal.halt("test reason")
    monkeypatch.setattr("trending_basket.execution.cli.demo_settings", lambda: settings)
    result = CliRunner().invoke(app, ["run", "resume"], input="n\n")
    assert result.exit_code != 0 and "test reason" in result.output
    assert Journal(journal.directory, journal.clock).state["halt"]
    # Confirmed recovery never adopts a foreign position.
    api.put_position("1")
    with pytest.raises(Halted):
        resume_checkpoint(settings, executor)
    assert not mutations(api)
    api.positions.clear()
    api.stops.clear()
    resume_checkpoint(settings, executor)
    assert journal.state["halt"] is None


def test_preview_checkpoint_and_telegram_failure_do_not_touch_execution_state(tmp_path):
    settings, _, _, journal, _, _ = setup_demo(tmp_path)
    journal.save()
    before = journal.path.read_bytes()
    preview = PreviewJournal(journal)
    preview.halt("preview only")
    assert journal.path.read_bytes() == before
    settings = settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("VERY-SECRET-TOKEN"),
            "telegram_chat_id": SecretStr("42"),
        }
    )
    notifier = Notifier(
        settings,
        journal,
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="VERY-SECRET-TOKEN")),
    )
    assert not notifier.send("test")
    assert not journal.state["halt"]
    assert "VERY-SECRET-TOKEN" not in (journal.directory / "events.jsonl").read_text()
    assert "VERY-SECRET-TOKEN" not in format_settings(settings)


def test_decision_boundary_three_minute_delay():
    midnight = START - 300000
    assert decision_day(midnight + 179999) == midnight - 86400000
    assert decision_day(midnight + 180000) == midnight


def test_journal_process_lock_prevents_double_owner(tmp_path):
    _, clock, _, journal, _, _ = setup_demo(tmp_path)
    with (
        journal.locked(),
        pytest.raises(Halted, match="another demo process"),
        Journal(journal.directory, clock).locked(),
    ):
        pytest.fail("second process acquired lock")


def test_decimal_reduction_never_leaves_more_than_target():
    rules = ExchangeRules.from_api(
        INSTRUMENT | {"lotSizeFilter": INSTRUMENT["lotSizeFilter"] | {"minOrderQty": "1"}}
    )
    delta, reason = planned_delta(199, D("2"), D("100"), rules, slippage_bps=0)
    assert delta == D(-1) and reason == "reduction_minimum_adjustment"
    assert (2 + delta) * 100 <= 199


def test_no_holdout_input_in_risk_configuration():
    document = json.loads(Path("docs/reports/T006-risk-level.json").read_text())
    assert set(document["runs"]) == {"dev_exact", "val_exact", "val_exchange"}
    assert document["risk_per_symbol_frac"] == 0.0065


def test_timeout_before_acceptance_retries_same_id_after_lookup(tmp_path):
    _, _, api, _, client, executor = setup_demo(tmp_path)
    posted = []
    original = api.__call__

    def transport(request):
        if request.url.path == "/v5/order/create":
            posted.append(json.loads(request.content)["orderLinkId"])
            if len(posted) == 1:
                raise httpx.ReadTimeout("not accepted", request=request)
            assert any(
                r[1] == "/v5/order/history" and r[2].get("orderLinkId") == posted[0]
                for r in api.requests
            )
        return original(request)

    client.http.close()
    client.http = httpx.Client(base_url=DEMO_URL, transport=httpx.MockTransport(transport))
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    assert len(posted) == 2 and len(set(posted)) == 1 and len(api.orders) == 1


def test_stop_fill_after_restart_recovers_exchange_generated_id(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api)
    journal.state["positions"][SYMBOL]["decision_price"] = 100
    journal.state["stop_ids"] = []
    journal.state["stop_intents"] = {SYMBOL: "90"}
    journal.state["last_poll_ms"] = START - 1000
    stop = api.stops.pop(SYMBOL)
    api.positions.clear()
    api.orders["history-stop"] = stop | {"orderStatus": "Filled"}
    api.fills.append(
        dict(
            symbol=SYMBOL,
            orderId=stop["orderId"],
            orderLinkId="",
            execId="stop-fill",
            execType="Trade",
            execTime=str(START),
            execPrice="89",
            execQty="2",
            execFee=".0979",
            side="Sell",
        )
    )
    executor.reconcile()
    assert not journal.state["positions"]
    assert journal.state["recent_exits"] == [{"symbol": SYMBOL, "time_ms": START, "reason": "stop"}]
    fill = json.loads((journal.directory / "fills.jsonl").read_text().splitlines()[0])
    assert fill["decision_price"] == 100 and fill["reference_price"] == 90
    assert fill["slippage_bps"] == pytest.approx(1100)
    assert fill["reference_slippage_bps"] == pytest.approx(10000 / 90)
    assert not mutations(api)


def test_old_unobserved_intent_never_sends_yesterdays_signal(tmp_path, monkeypatch):
    _, clock, api, journal, client, executor = setup_demo(tmp_path)
    real_create = client.create_order
    monkeypatch.setattr(
        client,
        "create_order",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("power loss before network")),
    )
    with pytest.raises(RuntimeError):
        executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START - 300000)
    assert journal.state["pending_order"]
    monkeypatch.setattr(client, "create_order", real_create)
    clock.advance(86400000)
    with pytest.raises(Halted, match="stale decision"):
        executor.recover_order()
    assert not mutations(api, "/v5/order/create")


def test_successful_operation_resets_streak_but_lookup_does_not(tmp_path):
    _, _, _, journal, client, executor = setup_demo(tmp_path)
    journal.api_error()
    client.wallet()
    assert journal.state["api_errors"] == 1
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    assert journal.state["api_errors"] == 0


def test_order_history_pagination(tmp_path):
    settings, clock, _, _, _, _ = setup_demo(tmp_path)

    def transport(request):
        if request.url.path.endswith("time"):
            return httpx.Response(
                200, json={"retCode": 0, "result": {"timeNano": str(clock.now_ms() * 1000000)}}
            )
        cursor = request.url.params.get("cursor", "")
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [{"id": cursor or "first"}],
                    "nextPageCursor": "second" if not cursor else "",
                },
            },
        )

    client = BybitPrivateClient(settings, clock, transport=httpx.MockTransport(transport))
    assert client.pages("/v5/order/history", category="linear") == [
        {"id": "first"},
        {"id": "second"},
    ]


def test_real_demo_public_fixture_has_required_execution_filters():
    record = json.loads(Path("tests/fixtures/bybit/demo-public.json").read_text())
    row = record["responses"]["/v5/market/instruments-info"]["body"]["result"]["list"][0]
    rules = ExchangeRules.from_api(row)
    assert rules.quantity.qty_step == D("0.001")
    assert rules.max_market_qty == D("150") and rules.tick_size == D("0.1")
    assert record["private_data"] is False


def test_mismatch_delta_uses_exchange_quantity_then_refuses_to_submit(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api, "2")
    journal.state["positions"][SYMBOL]["quantity"] = "3"
    with pytest.raises(Halted, match="mismatch"):
        executor.rebalance(SYMBOL, TargetPosition(190, 90), 100, 100, START)
    event = json.loads((journal.directory / "events.jsonl").read_text().splitlines()[0])
    assert event["held_quantity"] == "2"
    assert D(event["requested_delta"]) == D("-.1")
    assert not mutations(api, "/v5/order/create")


def test_existing_full_stop_is_confirmed_without_needless_mutation(tmp_path):
    _, _, api, journal, _, executor = setup_demo(tmp_path)
    own_position(journal, api)
    api.stop_error = 10016  # An already valid exchange stop does not require a successful rewrite.
    executor.confirm_stop(SYMBOL, 90)
    assert not mutations(api)
    assert journal.state["positions"][SYMBOL]["unprotected_since_ms"] is None
    assert not journal.state["halt"]
