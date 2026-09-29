"""Fail closed before loading data and append an audit record for explicit access."""

import json
from datetime import date
from pathlib import Path

import pytest
from backtest_support import START
from typer.testing import CliRunner

from trending_basket.backtest import cli
from trending_basket.backtest.config import load_experiment
from trending_basket.backtest.periods import authorize_period, period_dates, require_access
from trending_basket.cli import app
from trending_basket.clock import ManualClock


def experiment(tmp_path, period, strategy="trend_basket"):
    path = tmp_path / "test.toml"
    path.write_text(
        f'[run]\nname="gated"\nstrategy="{strategy}"\nperiod="{period}"\nuniverse="test"\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("period", ["val", "holdout", "full"])
def test_cli_missing_flag_fails_before_reading_data(tmp_path, monkeypatch, period):
    path = experiment(tmp_path, period)
    monkeypatch.setattr(cli, "load_inputs", lambda *_: pytest.fail("protected data read"))
    monkeypatch.setenv("TB_REPORTS_DIR", str(tmp_path / "reports"))
    result = CliRunner().invoke(app, ["backtest", "run", str(path)])
    assert result.exit_code == 1
    assert "requires --allow-" in result.output
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    "period,flag",
    [("val", "--allow-val"), ("holdout", "--allow-holdout"), ("full", "--allow-holdout")],
)
def test_cli_authorized_attempt_is_logged_before_data_read(tmp_path, monkeypatch, period, flag):
    path = experiment(tmp_path, period)
    report = tmp_path / "reports"
    monkeypatch.setenv("TB_REPORTS_DIR", str(report))

    def stop_after_log(*_):
        entries = [
            json.loads(line)
            for line in (report / "period-access-log.jsonl").read_text().splitlines()
        ]
        assert entries[-1]["period"] == period
        assert entries[-1]["commit"]
        assert entries[-1]["experiment"] == "gated"
        raise ValueError("synthetic stop after admission")

    monkeypatch.setattr(cli, "load_inputs", stop_after_log)
    for attempt in range(2):
        override = ["--override-holdout-lock"] if attempt and period != "val" else []
        result = CliRunner().invoke(
            app, ["backtest", "run", str(path), flag, *override], input="y\n"
        )
        assert "synthetic stop after admission" in result.output
    assert len((report / "period-access-log.jsonl").read_text().splitlines()) == 2


def test_holdout_declined_and_wrong_flag_never_reads_data(tmp_path, monkeypatch):
    path = experiment(tmp_path, "holdout")
    monkeypatch.setattr(cli, "load_inputs", lambda *_: pytest.fail("protected data read"))
    monkeypatch.setenv("TB_REPORTS_DIR", str(tmp_path / "reports"))
    for flags, answer in [(["--allow-holdout"], "n\n"), (["--allow-val"], "y\n")]:
        result = CliRunner().invoke(app, ["backtest", "run", str(path), *flags], input=answer)
        assert result.exit_code == 1
    assert not (tmp_path / "reports").exists()


def test_dates_and_engine_admission(tmp_path):
    assert period_dates("dev") == (date(2021, 11, 1), date(2024, 6, 30))
    assert period_dates("val") == (date(2024, 7, 1), date(2025, 9, 30))
    # Clock is midnight 2026-09-28: September 28 is NOT yet closed.
    clock = ManualClock(1790553600000)
    assert period_dates("holdout", clock) == (date(2025, 10, 1), date(2026, 9, 27))
    with pytest.raises(ValueError, match="authorization"):
        require_access("V1", "trend_basket", "val", None)
    access = authorize_period(
        experiment="V1",
        strategy="trend_basket",
        period="val",
        allow_val=True,
        allow_holdout=False,
        confirmed=False,
        reports_dir=tmp_path,
        clock=ManualClock(START),
        commit="abc",
    )
    require_access("V1", "trend_basket", "val", access)
    with pytest.raises(ValueError):
        require_access("V2", "trend_basket", "val", access)
    require_access("btc", "buy_and_hold_btc", "full", None)


@pytest.mark.parametrize(
    "change",
    ['period="unknown"', "", 'period="dev"\nstart="2021-01-01"', 'period="dev"\nend="2022-01-01"'],
)
def test_manual_dates_and_missing_period_rejected(tmp_path, change):
    path = experiment(tmp_path, "dev")
    path.write_text(path.read_text().replace('period="dev"', change))
    with pytest.raises(ValueError):
        load_experiment(path)


def test_seven_preregistered_variants_and_disabled_btc_limits():
    for name in ("V1", "V2", "V3", "V4", "V5", "V1-10k", "V2-10k"):
        run = load_experiment(Path("experiments") / f"{name}.toml")
        assert run.run.period == "dev"
        assert run.run.start == date(2021, 11, 1) and run.run.end == date(2024, 6, 30)
        assert run.limits.enabled
    assert not load_experiment(Path("experiments/bh_btc.toml")).limits.enabled


@pytest.mark.parametrize(
    "first_period,second_period", [("holdout", "holdout"), ("full", "holdout"), ("holdout", "full")]
)
def test_second_candidate_locked_before_data_read_and_override_warns(
    tmp_path, monkeypatch, first_period, second_period
):
    report = tmp_path / "reports"
    authorize_period(
        experiment="H2",
        strategy="trend_basket",
        period=first_period,
        allow_val=False,
        allow_holdout=True,
        confirmed=True,
        reports_dir=report,
        clock=ManualClock(START),
        commit="first",
    )
    path = experiment(tmp_path, second_period)
    monkeypatch.setenv("TB_REPORTS_DIR", str(report))
    monkeypatch.setattr(cli, "load_inputs", lambda *_: pytest.fail("protected data read"))
    args = ["backtest", "run", str(path), "--allow-holdout"]
    result = CliRunner().invoke(app, args, input="y\n")
    assert result.exit_code == 1 and "requires --override-holdout-lock" in result.output
    journal = report / "period-access-log.jsonl"
    assert len(journal.read_text().splitlines()) == 1
    assert not (report / ".period-access.lock").exists()

    def admitted(*_):
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        assert len(rows) == 2 and "OVERRIDDEN" in rows[-1]["warning"]
        raise ValueError("synthetic admitted override")

    monkeypatch.setattr(cli, "load_inputs", admitted)
    result = CliRunner().invoke(app, [*args, "--override-holdout-lock"], input="y\n")
    assert "synthetic admitted override" in result.output and "WARNING" in result.output


def test_existing_holdout_does_not_block_benchmark_and_corrupt_journal_fails_closed(tmp_path):
    journal = tmp_path / "period-access-log.jsonl"
    journal.write_text('{"period":"holdout","strategy":"trend_basket"}\n')
    kwargs = dict(
        experiment="btc",
        strategy="buy_and_hold_btc",
        period="holdout",
        allow_val=False,
        allow_holdout=False,
        confirmed=False,
        reports_dir=tmp_path,
        clock=ManualClock(START),
        commit="abc",
    )
    authorize_period(**kwargs)
    assert len(journal.read_text().splitlines()) == 1
    journal.write_text('{"unfinished":')
    with pytest.raises(ValueError):
        authorize_period(
            **(kwargs | dict(strategy="trend_basket", allow_holdout=True, confirmed=True))
        )
    assert not (tmp_path / ".period-access.lock").exists()
