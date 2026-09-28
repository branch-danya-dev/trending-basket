"""Smoke tests: the package imports and the CLI starts."""

from __future__ import annotations

from typer.testing import CliRunner

import trending_basket
from trending_basket.cli import app

runner = CliRunner()


def test_package_imports() -> None:
    assert trending_basket.__version__


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == trending_basket.__version__
