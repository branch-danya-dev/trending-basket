"""Universe CLI exercised offline with cached synthetic input."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner
from universe_test_support import JUNE_MS, seed_universe_inputs

from trending_basket.cli import app
from trending_basket.clock import ManualClock
from trending_basket.universe import cli

runner = CliRunner()


def test_candidate_build_report_and_show_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "SystemClock", lambda: ManualClock(JUNE_MS))
    out = tmp_path / "candidate-list.txt"
    result = runner.invoke(
        app, ["universe", "candidates", "--out", str(out), "--exclusions-file", str(exclusions)]
    )
    assert result.exit_code == 0, result.output
    assert out.read_text().splitlines() == ["BTCUSDT", "ETHUSDT", "MISSINGUSDT"]
    result = runner.invoke(
        app,
        [
            "universe",
            "build",
            "--name",
            "core15",
            "--since",
            "2024-05-01",
            "--exclusions-file",
            str(exclusions),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "missing caches (1): MISSINGUSDT" in result.output
    assert "survivorship_bias=true" in result.output
    result = runner.invoke(app, ["universe", "report", "core15"])
    assert result.exit_code == 0, result.output
    assert "2024-05-01 | 2 | BTCUSDT,ETHUSDT | -" in result.output
    assert "2024-06-01 | 0 | - | BTCUSDT,ETHUSDT" in result.output
    assert "underfilled" in result.output
    result = runner.invoke(app, ["universe", "show", "core15", "--at", "2024-05-01"])
    assert result.exit_code == 0
    assert "BTCUSDT" in result.output
    result = runner.invoke(app, ["universe", "show", "core15", "--at", "2024-06-01"])
    assert result.exit_code == 0
    assert "empty universe" in result.output
    result = runner.invoke(app, ["universe", "show", "core15", "--at", "2024-05-02"])
    assert result.exit_code == 1


def test_missing_snapshot_fails_with_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["universe", "candidates", "--out", str(tmp_path / "list.txt")])
    assert result.exit_code == 1
    assert "sync instruments" in result.output
