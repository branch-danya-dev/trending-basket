"""ADR-016 thresholds, deterministic CLI decisions and immutable evidence."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trending_basket.backtest.config import load_experiment
from trending_basket.backtest.periods import period_dates
from trending_basket.cli import app
from trending_basket.clock import ManualClock
from trending_basket.research.decisions import CANDIDATES, holdout_verdict, select_candidate

CLOCK = ManualClock(1790640000000)  # 2026-09-29 00:00 UTC


def metrics(**changes):
    return (
        dict(
            quantity_mode="exact",
            closed_positions=100,
            max_drawdown_frac=-0.20,
            sharpe=0.5,
            total_return_frac=0.15,
            annual_volatility_frac=0.1,
        )
        | changes
    )


def report(tmp_path, ident, period="val", values=None):
    directory = tmp_path / f"{ident}-{period}"
    directory.mkdir()
    config = load_experiment(Path("experiments/val") / f"{ident}.toml").model_dump(mode="json")
    start, end = period_dates(period, CLOCK)
    config["run"].update(period=period, start=start.isoformat(), end=end.isoformat())
    (directory / "metrics.json").write_text(json.dumps(values or metrics()), encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(
            dict(
                resolved_config=config,
                quantity_mode="exact",
                created_at_ms=CLOCK.now_ms(),
                git={"commit": "test", "dirty": False},
            )
        ),
        encoding="utf-8",
    )
    return directory


def select_cli(tmp_path, changes=None):
    directories = [
        report(tmp_path, ident, values=(changes or {}).get(ident)) for ident in sorted(CANDIDATES)
    ]
    output = tmp_path / "selection.json"
    args = ["research", "select", "--period", "val", "--output", str(output)]
    for directory in directories:
        args.extend(["--candidates", str(directory)])
    return CliRunner().invoke(app, args), output, args


def test_cli_selection_all_exclusions_maximum_and_full_evidence(tmp_path):
    result, output, args = select_cli(
        tmp_path,
        {
            "V1x": metrics(closed_positions=99, sharpe=9),
            "V2x": metrics(max_drawdown_frac=-0.2000000001, sharpe=8),
            "V3x": metrics(sharpe=None),
            "V4x": metrics(sharpe=1.1),
            "H2": metrics(sharpe=0.9),
        },
    )
    assert result.exit_code == 0, result.output
    decision = json.loads(output.read_text())
    assert decision["selected_id"] == "V4x" and decision["holdout_allowed"]
    rows = {row["id"]: row for row in decision["candidates"]}
    assert rows["V1x"]["exclusion_reasons"] == ["closed_positions < 100"]
    assert rows["V2x"]["exclusion_reasons"] == ["max_drawdown_frac < -0.20"]
    assert rows["V3x"]["exclusion_reasons"] == ["sharpe undefined"]
    assert len(decision["sources"]["V4x"]["metrics_sha256"]) == 64
    before = output.read_bytes()
    assert CliRunner().invoke(app, args).exit_code == 1
    assert output.read_bytes() == before


def test_cli_exact_tie_uses_id_not_argument_order(tmp_path):
    result, output, _ = select_cli(tmp_path)
    assert result.exit_code == 0
    assert json.loads(output.read_text())["selected_id"] == "H1"
    decision = select_candidate({"V2x": metrics(), "H2": metrics()})
    assert decision["selected_id"] == "H2"


def test_cli_no_eligible_forbids_holdout(tmp_path):
    result, output, _ = select_cli(
        tmp_path, {ident: metrics(closed_positions=99) for ident in CANDIDATES}
    )
    assert result.exit_code == 0
    decision = json.loads(output.read_text())
    assert decision["selected_id"] is None and not decision["holdout_allowed"]
    verdict = CliRunner().invoke(
        app,
        [
            "research",
            "holdout-verdict",
            "--selection",
            str(output),
            "--candidate",
            "never-read",
            "--btc",
            "never-read",
        ],
    )
    assert verdict.exit_code == 1 and "does not authorize" in verdict.output


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "wrong_period", "exchange", "parameters"]
)
def test_cli_rejects_wrong_selection_inputs(tmp_path, mutation):
    result, output, args = select_cli(tmp_path)
    assert result.exit_code == 0
    output.unlink()
    if mutation == "missing":
        args = args[:-2]
    elif mutation == "duplicate":
        args.extend(args[-2:])
    elif mutation == "wrong_period":
        args[args.index("val")] = "holdout"
    else:
        path = tmp_path / "H1-val" / "manifest.json"
        manifest = json.loads(path.read_text())
        if mutation == "exchange":
            manifest["resolved_config"]["execution"]["quantity_mode"] = "exchange"
        else:
            manifest["resolved_config"]["strategy_params"]["exit_ratio"] = 0.6
        path.write_text(json.dumps(manifest))
    assert CliRunner().invoke(app, args).exit_code == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "candidate_changes,btc_changes,passed,failed_key",
    [
        ({}, {}, True, None),
        ({"sharpe": 0}, {}, False, "positive_sharpe"),
        ({"sharpe": None}, {}, False, "positive_sharpe"),
        ({"max_drawdown_frac": -0.2500000001}, {}, False, "drawdown"),
        ({"max_drawdown_frac": -0.25}, {}, True, None),
        ({"total_return_frac": 0.1}, {}, False, "volatility_matched_btc"),
        ({}, {"annual_volatility_frac": 0}, False, "volatility_matched_btc"),
        ({}, {"annual_volatility_frac": None}, False, "volatility_matched_btc"),
    ],
)
def test_cli_holdout_each_condition_and_boundaries(
    tmp_path, candidate_changes, btc_changes, passed, failed_key
):
    result, selection, _ = select_cli(tmp_path)
    assert result.exit_code == 0
    candidate = report(tmp_path, "H1", "holdout", metrics(**candidate_changes))
    btc_metrics = metrics(total_return_frac=0.4, annual_volatility_frac=0.4) | btc_changes
    btc = report(tmp_path, "bh_btc", "holdout", btc_metrics)
    output = tmp_path / "verdict.json"
    result = CliRunner().invoke(
        app,
        [
            "research",
            "holdout-verdict",
            "--candidate",
            str(candidate),
            "--btc",
            str(btc),
            "--selection",
            str(selection),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    verdict = json.loads(output.read_text())
    assert verdict["passed"] is passed
    assert [key for key, value in verdict["checks"].items() if not value["passed"]] == (
        [failed_key] if failed_key else []
    )


@pytest.mark.parametrize("mutation", ["candidate", "dates", "parameters", "selection"])
def test_cli_verdict_rejects_substituted_candidate_window_or_rules(tmp_path, mutation):
    _, selection, _ = select_cli(tmp_path)
    candidate = report(tmp_path, "H2" if mutation == "candidate" else "H1", "holdout")
    btc = report(tmp_path, "bh_btc", "holdout")
    if mutation in {"dates", "parameters"}:
        path = (btc if mutation == "dates" else candidate) / "manifest.json"
        value = json.loads(path.read_text())
        if mutation == "dates":
            value["resolved_config"]["run"]["end"] = "2026-09-27"
            value["created_at_ms"] -= 86400000
        else:
            value["resolved_config"]["strategy_params"]["exit_ratio"] = 0.8
        path.write_text(json.dumps(value))
    if mutation == "selection":
        value = json.loads(selection.read_text())
        value["selected_id"] = "H2"
        selection.write_text(json.dumps(value))
    result = CliRunner().invoke(
        app,
        [
            "research",
            "holdout-verdict",
            "--candidate",
            str(candidate),
            "--btc",
            str(btc),
            "--selection",
            str(selection),
            "--output",
            str(tmp_path / "verdict.json"),
        ],
    )
    assert result.exit_code == 1


def test_unevaluable_criterion_stops_instead_of_interpreting():
    with pytest.raises(ValueError, match="cannot be evaluated"):
        select_candidate({"H1": metrics(max_drawdown_frac=None)})
    with pytest.raises(ValueError, match="cannot be evaluated"):
        holdout_verdict(metrics(annual_volatility_frac=None), metrics())
    # Negative BTC returns retain the signed comparison; no absolute-value reinterpretation.
    verdict = holdout_verdict(
        metrics(total_return_frac=-0.01),
        metrics(total_return_frac=-0.4, annual_volatility_frac=0.4),
    )
    assert verdict["checks"]["volatility_matched_btc"]["passed"]
