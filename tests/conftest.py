"""Shared pytest fixtures: network lockdown and a clean TB_* environment."""

from __future__ import annotations

import os
import socket

import pytest


class NetworkBlockedError(RuntimeError):
    """Raised when test code attempts to open a network connection."""


def _blocked_connect(*_args: object, **_kwargs: object) -> None:
    raise NetworkBlockedError("network access is not allowed in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("TB_"):
            monkeypatch.delenv(key, raising=False)
