"""Tests for Settings loading and formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

from trending_basket.config import format_settings, load_settings


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.mode == "research"
    assert settings.log_level == "INFO"
    assert settings.bybit_api_key is None


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TB_MODE", "paper")
    monkeypatch.setenv("TB_LOG_LEVEL", "DEBUG")
    settings = load_settings()
    assert settings.mode == "paper"
    assert settings.log_level == "DEBUG"


def test_unknown_key_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TB_FOO", "bar")
    with pytest.raises(ValueError, match="TB_FOO"):
        load_settings()


def test_live_mode_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TB_MODE", "live")
    with pytest.raises(ValueError, match="live"):
        load_settings()


def test_secrets_masked_in_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TB_BYBIT_API_KEY", "super-secret-key")
    monkeypatch.setenv("TB_BYBIT_API_SECRET", "super-secret-secret")
    settings = load_settings()
    output = format_settings(settings)
    assert "super-secret-key" not in output
    assert "super-secret-secret" not in output
    assert "***" in output


def test_unset_secrets_shown_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    output = format_settings(settings)
    assert "<unset>" in output
