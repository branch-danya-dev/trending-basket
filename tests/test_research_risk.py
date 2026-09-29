"""Risk rule only scales dev/val volatility, never selects by holdout results."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from trending_basket.cli import app
from trending_basket.research.risk import baseline_returns, risk_from_volatility


@pytest.mark.parametrize(
    "vol,expected", [(0.12, 0.005), (0.08, 0.0075), (0.09, 0.0065), (0.02, 0.01)]
)
def test_risk_scale_floor_and_cap(vol, expected):
    assert risk_from_volatility(vol) == expected


@pytest.mark.parametrize("vol", [0, float("nan"), float("inf"), -0.2, 100])
def test_undefined_or_zero_risk_stops(vol):
    with pytest.raises(ValueError):
        risk_from_volatility(vol)


def test_holdout_rejected_before_reading(tmp_path):
    with pytest.raises(ValueError, match="forbids holdout"):
        baseline_returns(tmp_path / "must-not-read", "holdout")


def test_risk_cli_requires_explicit_val_and_never_overwrites(tmp_path):
    args = [
        "research",
        "risk-level",
        "--dev",
        "absent",
        "--val",
        "absent",
        "--output",
        str(tmp_path / "risk.json"),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1 and "requires --allow-val" in result.output
    Path(tmp_path / "risk.json").write_text("{}")
    result = CliRunner().invoke(app, [*args, "--allow-val"])
    assert result.exit_code == 1 and "already exists" in result.output
