"""Run continuity regressions with real journals and a simulated exchange."""

import copy
import json
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from demo_support import START, SYMBOL, mutations, setup_demo
from test_demo_cycle import DAY, market
from typer.testing import CliRunner

from trending_basket.cli import app
from trending_basket.execution import capital
from trending_basket.execution.continuity import RECOVERY_MS, Heartbeat, Retry, recover
from trending_basket.execution.cycle import risk_config, run_cycle
from trending_basket.execution.journal import Halted, Journal, Notifier
from trending_basket.execution.ledger import Ledger, LedgerError, atomic_json
from trending_basket.execution.live_executor import LiveExecutor
from trending_basket.execution.replay import reconstruct_equity, replay_strategy, verify_capital
from trending_basket.execution.runs import active_directory, close, create, validate
from trending_basket.execution.service import install_command, launcher_text, task_xml
from trending_basket.strategies.base import TargetPosition


def managed(tmp_path):
    settings, clock, api, _, client, old = setup_demo(tmp_path)
    journal = create(settings, clock, client, "test")
    capital.initialize(journal, 1000, clock.now_ms())
    notifier = Notifier(settings, journal)
    notifier.silent = True
    executor = LiveExecutor(client, journal, notifier, old.rules)
    return settings, clock, api, journal, client, executor


@pytest.mark.parametrize("tail", [b'{"half":', b'{"seq":99}', b"not-json\n"])
def test_torn_tail_preserved_and_repaired(tmp_path, tail):
    _, clock, _, journal, _, _ = managed(tmp_path)
    journal.append("events", kind="before")
    with journal.ledger.path.open("ab") as stream:
        stream.write(tail)
    reopened = Journal(journal.directory, clock)
    with reopened.locked():
        assert reopened.read_rows("events")[-1]["kind"] == "journal_tail_repaired"
    assert next(journal.directory.glob("*.corrupt-*")).read_bytes() == tail
    Ledger(journal.directory).verify(clock.now_ms())


def test_middle_hash_change_halts_without_exchange_mutation(tmp_path):
    _, clock, api, journal, _, _ = managed(tmp_path)
    journal.append("events", kind="original")
    journal.append("events", kind="after")
    path = journal.ledger.path
    path.write_bytes(path.read_bytes().replace(b'"original"', b'"tampered"'))
    with pytest.raises(Halted, match="chain mismatch"), Journal(journal.directory, clock).locked():
        pass
    assert not mutations(api)


def test_seq_gap_and_snapshot_tampering_rejected(tmp_path):
    _, clock, _, journal, _, _ = managed(tmp_path)
    original = journal.ledger.path.read_bytes()
    journal.ledger.path.write_bytes(b"\n".join(original.splitlines()[1:]) + b"\n")
    with pytest.raises(LedgerError, match="seq 1"):
        Ledger(journal.directory).verify(clock.now_ms())
    journal.ledger.path.write_bytes(original)
    state = json.loads(journal.path.read_text())
    state["peak_equity_usd"] = 2000
    atomic_json(journal.path, state)
    with pytest.raises(Halted, match="snapshot"), Journal(journal.directory, clock).locked():
        pass


def test_crash_after_wal_before_replace_restores_committed_state(tmp_path, monkeypatch):
    _, clock, _, journal, _, _ = managed(tmp_path)
    old = journal.path.read_bytes()
    import trending_basket.execution.ledger as module

    def crash(*args):
        raise OSError("power loss")

    with monkeypatch.context() as m:
        m.setattr(module.os, "replace", crash)
        journal.state["peak_equity_usd"] = 1001
        with pytest.raises(OSError):
            journal.save()
    assert journal.path.read_bytes() == old
    reopened = Journal(journal.directory, clock)
    with reopened.locked():
        assert reopened.state["peak_equity_usd"] == 1001


def test_active_identity_config_account_and_code_guards(tmp_path, monkeypatch):
    settings, clock, _, journal, client, _ = managed(tmp_path)
    assert active_directory(settings) == journal.directory
    reopened = Journal(active_directory(settings), clock)
    with reopened.locked():
        validate(reopened, settings, client)
        with pytest.raises(Halted, match="configuration"):
            validate(reopened, settings.model_copy(update={"allocated_capital_usd": 2000}), client)
        previous = reopened.state["code_identity"]
        changed = dict(previous, commit="next", critical_hash="changed")
        monkeypatch.setattr("trending_basket.execution.runs.code_identity", lambda: changed)
        with pytest.raises(Halted, match="allow-code-change"):
            validate(reopened, settings, client)
        validate(reopened, settings, client, allow_code_change=True)
        assert reopened.read_rows("events")[-1]["substantial"]
    with pytest.raises(Halted, match="already exists"):
        create(settings, clock, client, "another")


def test_readonly_start_required_and_close_does_not_trade(tmp_path, monkeypatch):
    settings, _, api, journal, _, executor = managed(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    count = len(mutations(api))
    with pytest.raises(Halted, match="positions"):
        close(journal, settings)
    assert len(mutations(api)) == count
    executor.rebalance(SYMBOL, TargetPosition(0), 100, 100, START)
    close(journal, settings)
    monkeypatch.setattr("trending_basket.execution.cli.demo_settings", lambda: settings)
    result = CliRunner().invoke(app, ["run", "--once"])
    assert result.exit_code == 1 and "tb run start" in result.output


def test_recovery_funding_and_fills_are_idempotent(tmp_path):
    _, clock, api, journal, client, executor = managed(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    clock.advance(600000)
    api.transactions.append(
        dict(
            id="fund-1",
            symbol=SYMBOL,
            type="SETTLEMENT",
            category="linear",
            currency="USDT",
            transactionTime=str(clock.now_ms()),
            funding="-0.03",
            size="1.9",
        )
    )
    client.read_only = True
    recover(executor)
    before = copy.deepcopy(journal.state["capital"])
    row_count = len(journal.read_rows("exchange"))
    recover(executor)
    assert journal.state["capital"] == before
    assert len(journal.read_rows("exchange")) == row_count
    assert len(journal.read_rows("funding")) == 1
    assert journal.read_rows("funding")[0]["recovered"]
    assert verify_capital(journal, executor.actual())["funding_usd"] == -0.03


def test_stop_during_downtime_recovered_once_and_protective_exit_separate(tmp_path):
    _, clock, api, journal, client, executor = managed(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    clock.advance(600000)
    api.fills.append(
        dict(
            execId="offline-stop",
            orderId=f"stop-{SYMBOL}",
            orderLinkId="",
            execType="Trade",
            execTime=str(clock.now_ms()),
            symbol=SYMBOL,
            side="Sell",
            execQty="1.9",
            execPrice="89",
            execFee=".1",
            feeCurrency="USDT",
        )
    )
    api.positions.clear()
    api.stops.clear()
    client.read_only = True
    first = recover(executor)
    second = recover(executor)
    assert first["stops"] == 1 and second["stops"] == 0
    assert len(journal.state["recent_exits"]) == 1
    assert journal.read_rows("fills")[-1]["reason"] == "stop"
    assert journal.read_rows("fills")[-1]["recovered"]
    assert not journal.state["positions"]
    assert verify_capital(journal, executor.actual())["realized_pnl_usd"] == -20.9


def test_gap_blocks_orders_preserves_stop_and_requires_acceptance(tmp_path):
    _, clock, api, journal, client, executor = managed(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    clock.advance(RECOVERY_MS + 1)
    count = len(mutations(api))
    with pytest.raises(Halted, match="unrecoverable_gap"):
        recover(executor)
    assert api.positions[SYMBOL]["stopLoss"] == "90"
    assert len(mutations(api)) == count
    client.read_only = True
    recover(executor, accept_gap=True)
    assert journal.state["accepted_gap"] and journal.state["t006b_reset_ms"] == clock.now_ms()


def test_manual_stop_change_detected_without_overwriting_it(tmp_path):
    _, _, api, journal, client, executor = managed(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    api.put_stop(SYMBOL, "91")
    client.read_only = True
    with pytest.raises(Halted, match="stop differs"):
        recover(executor)
    assert api.positions[SYMBOL]["stopLoss"] == "91" and journal.state["t006b_reset_ms"]


def test_heartbeat_restart_sleep_and_retry_longer_than_day(tmp_path):
    _, clock, _, journal, _, _ = managed(tmp_path)
    hb = Heartbeat(journal, 0)
    assert hb.startup() == 0
    clock.advance(600000)
    assert hb.tick(1000)
    assert journal.state["downtime_ms"] == 600000
    clock.advance(600000)
    hb2 = Heartbeat(journal, 0)
    assert hb2.startup() == 600000
    assert journal.state["restart_count"] == 1
    retry = Retry()
    for _ in range(300):
        delay, _notice = retry.failure(journal, clock.now_ms(), jitter=0.5)
        assert delay <= 300
        clock.advance(300000)
    assert not journal.state["halt"]
    retry.success(journal, clock.now_ms())
    assert journal.read_rows("events")[-1]["kind"] == "network_recovered"


def test_independent_strategy_replay_detects_changed_candle_and_three_missed_days(tmp_path):
    settings, clock, api, journal, client, executor = managed(tmp_path)
    inputs = market(executor)
    from backtest_support import START as HISTORY_START

    day = HISTORY_START + 400 * DAY
    clock.set(day + 300000)
    journal.state["capital"]["start_ms"] = clock.now_ms()
    journal.state["capital"]["funding_checked_ms"] = clock.now_ms()
    api.last_price = str(inputs.data.close_price(SYMBOL, day))
    run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    config, _, _ = risk_config(settings.demo_risk_file)
    replay_strategy(journal, config, inputs)
    count = len(mutations(api, "/v5/order/create"))
    clock.advance(3 * DAY)
    api.last_price = str(inputs.data.close_price(SYMBOL, day + 3 * DAY))
    api.positions[SYMBOL]["markPrice"] = api.last_price
    result = run_cycle(settings, client, journal, executor.notifier, inputs, clock)
    assert result["late"] and result["lateness_ms"] == 120000
    assert len([r for r in journal.read_rows("decisions") if r.get("status") == "completed"]) == 2
    assert len(journal.state["strategy_steps"]) == 4
    assert len(mutations(api, "/v5/order/create")) - count <= 1
    replay_strategy(journal, config, inputs)
    journal.state["strategy_states"][SYMBOL]["atr"] *= 2
    with pytest.raises(Halted, match="snapshot differs"):
        replay_strategy(journal, config, inputs)


def test_historical_equity_from_exchange_cashflows_and_daily_closes(tmp_path):
    _, clock, _, journal, _, executor = managed(tmp_path)
    executor.rebalance(SYMBOL, TargetPosition(200, 90), 100, 100, START)
    from backtest_support import candle, store

    inputs = market(executor)
    from dataclasses import replace

    from trending_basket.domain.types import Interval

    day = (START // DAY + 1) * DAY
    row = replace(
        candle(0), symbol=SYMBOL, interval=Interval.D1, open_time_ms=day - DAY, close=110, high=110
    )
    inputs.data = store([row])
    clock.set(day + 10000)
    reconstruct_equity(journal, inputs, clock.now_ms())
    value = journal.read_rows("equity")[-1]
    assert value["reconstructed"] and value["equity_usd"] == pytest.approx(1018.8955)
    reconstruct_equity(journal, inputs, clock.now_ms())
    assert len([r for r in journal.read_rows("equity") if r.get("reconstructed")]) == 1


def test_history_windows_all_pages_and_scheduler_arguments(tmp_path):
    settings, clock, _, _, _, _ = setup_demo(tmp_path)
    from trending_basket.execution.bybit_private import BybitPrivateClient

    calls = []

    def transport(request):
        params = dict(request.url.params)
        if request.url.path == "/v5/market/time":
            return httpx.Response(
                200, json={"retCode": 0, "result": {"timeNano": str(clock.now_ms() * 1000000)}}
            )
        calls.append((request.url.path, params))
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [{"id": params.get("cursor", "first")}],
                    "nextPageCursor": "next" if "cursor" not in params else "",
                },
            },
        )

    client = BybitPrivateClient(settings, clock, transport=httpx.MockTransport(transport))
    result = client.history(START, START + 8 * DAY)
    assert all(len(rows) == 4 for rows in result.values())
    assert all(int(p["endTime"]) - int(p["startTime"]) < 7 * DAY for _, p in calls)
    project = Path("C:/project with spaces")
    xml = task_xml(project, project / "launch.ps1", "PC\\owner")
    tree = ElementTree.fromstring(xml)
    assert tree.find(".//{*}RestartOnFailure/{*}Interval").text == "PT1M"
    assert "--loop" in launcher_text(project, "C:/tools/uv.exe")
    args = install_command(project, project / "task.xml")
    assert args[:2] == ["schtasks", "/Create"] and args[-2:] == [str(project / "task.xml"), "/F"]


def test_verify_cli_exit_codes_and_no_private_writes(tmp_path, monkeypatch):
    settings, clock, api, journal, client, executor = managed(tmp_path)
    import trending_basket.execution.cli as module

    monkeypatch.setattr(module, "demo_settings", lambda: settings)
    monkeypatch.setattr(module, "SystemClock", lambda: clock)
    monkeypatch.setattr(module, "BybitPrivateClient", lambda *a, **kw: client)
    monkeypatch.setattr(module, "update_data", lambda *a: market(executor))
    valid = CliRunner().invoke(app, ["run", "verify"])
    assert valid.exit_code == 0, valid.output
    assert '"status": "consistent"' in valid.output
    assert not mutations(api)
    journal.ledger.path.write_bytes(
        journal.ledger.path.read_bytes().replace(b'"run_created"', b'"run_changed"')
    )
    invalid = CliRunner().invoke(app, ["run", "verify"])
    assert invalid.exit_code == 1 and "chain mismatch" in invalid.output
    assert not mutations(api)


def test_loop_survives_network_crossing_two_daily_decisions(tmp_path, monkeypatch):
    settings, clock, _api, journal, client, executor = managed(tmp_path)
    import trending_basket.execution.cli as module
    from trending_basket.execution.bybit_private import PrivateAPIError

    monkeypatch.setattr(module, "demo_settings", lambda: settings)
    monkeypatch.setattr(module, "SystemClock", lambda: clock)
    monkeypatch.setattr(clock, "monotonic_ms", lambda: clock.now_ms() - START, raising=False)
    monkeypatch.setattr(module, "BybitPrivateClient", lambda *a, **kw: client)
    monkeypatch.setattr(module, "validate_run", lambda *a, **kw: None)
    monkeypatch.setattr(module, "supervise", lambda *a: None)
    monkeypatch.setattr(module, "update_data", lambda *a: market(executor))
    tries, executed = [], []

    def verify(*args, **kwargs):
        tries.append(clock.now_ms())
        if len(tries) < 3:
            raise PrivateAPIError("/v5/execution/list", -1)
        return {"recovered": {"executions": 0, "stops": 0, "transactions": 0}}

    def cycle(*args, **kwargs):
        executed.append(module.decision_day(clock.now_ms()))
        return {"status": "completed", "late": True}

    def sleep(seconds):
        if executed:
            raise KeyboardInterrupt
        clock.advance(13 * 3600000)

    monkeypatch.setattr(module, "verify_current", verify)
    monkeypatch.setattr(module, "recover", lambda *a: {})
    monkeypatch.setattr(module, "run_cycle", cycle)
    monkeypatch.setattr(client, "sleep", sleep)
    result = CliRunner().invoke(app, ["run", "--loop"])
    assert len(tries) == 3 and len(executed) == 1, result.output
    assert executed == [module.decision_day(clock.now_ms())]
    assert executed[0] > module.decision_day(START)
    restored = Journal(journal.directory, clock)
    with restored.locked():
        assert not restored.state["halt"]
        assert restored.state["downtime_ms"] == 26 * 3600000


def test_strategy_stop_is_consumed_once_across_missed_bars(tmp_path):
    from backtest_support import START as HISTORY_START

    from trending_basket.execution.replay import strategy_step

    settings, _, _, journal, _, executor = managed(tmp_path)
    inputs = market(executor)
    config, _, _ = risk_config(settings.demo_risk_file)
    day = HISTORY_START + 400 * DAY
    strategy_step(journal, config, inputs, day, 1000)
    journal.state["recent_exits"] = [dict(symbol=SYMBOL, time_ms=day + 1, reason="stop")]
    strategy_step(journal, config, inputs, day + DAY, 1000)
    strategy_step(journal, config, inputs, day + 2 * DAY, 1000)
    steps = journal.read_rows("strategy")
    assert len(steps[-2]["context"]["recent_exits"]) == 1
    assert steps[-1]["context"]["recent_exits"] == []
    replay_strategy(journal, config, inputs)


def test_actual_candle_change_breaks_independent_replay(tmp_path):
    from dataclasses import replace

    from backtest_support import START as HISTORY_START
    from backtest_support import store

    from trending_basket.execution.replay import strategy_step

    settings, _, _, journal, _, executor = managed(tmp_path)
    inputs = market(executor)
    config, _, _ = risk_config(settings.demo_risk_file)
    day = HISTORY_START + 400 * DAY
    strategy_step(journal, config, inputs, day, 1000)
    changed = copy.copy(inputs)
    changed.data = store(
        [replace(row, high=row.high + 10) for rows in inputs.data.rows.values() for row in rows]
    )
    with pytest.raises(Halted, match="replay differs"):
        replay_strategy(journal, config, changed)


def test_universe_rebuild_starts_at_first_missed_month(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from backtest_support import universe

    import trending_basket.execution.cycle as module

    settings, clock, _, _, client, executor = managed(tmp_path)

    def stamp(month):
        return int(datetime(2025, month, 1, tzinfo=UTC).timestamp() * 1000)

    old = universe(schedule={stamp(1): [SYMBOL]})
    inputs = market(executor)
    seen = []
    path = tmp_path / "exists"
    path.touch()
    monkeypatch.setattr(module, "sync_instruments", lambda **kw: None)
    monkeypatch.setattr(module, "universe_paths", lambda *a: (path, path))
    monkeypatch.setattr(module, "load_universe", lambda *a: old)
    monkeypatch.setattr(module, "candidate_pool", lambda *a: SimpleNamespace(symbols=[]))
    monkeypatch.setattr(module, "sync_klines", lambda **kw: None)
    monkeypatch.setattr(module, "sync_funding", lambda **kw: None)
    monkeypatch.setattr(module, "save_universe", lambda *a: None)

    def build(**kwargs):
        seen.append(kwargs["since_ms"])
        return universe(schedule={stamp(2): [SYMBOL], stamp(3): [SYMBOL]})

    monkeypatch.setattr(module, "build_universe", build)
    monkeypatch.setattr(module, "load_inputs", lambda *a: inputs)
    # The synthetic market intentionally does not extend to March; inspect the rebuild first.
    with pytest.raises(ValueError, match="stale daily cache"):
        module._update_data(settings, stamp(3), set(), clock, client)
    assert seen == [stamp(2)]
