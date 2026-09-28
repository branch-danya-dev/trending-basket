"""Tests for ManualClock."""

from __future__ import annotations

import pytest

from trending_basket.clock import ManualClock


def test_initial_value() -> None:
    clock = ManualClock(1_000)
    assert clock.now_ms() == 1_000


def test_set_advances_time() -> None:
    clock = ManualClock(1_000)
    clock.set(2_000)
    assert clock.now_ms() == 2_000


def test_set_rejects_moving_backward() -> None:
    clock = ManualClock(2_000)
    with pytest.raises(ValueError, match="backward"):
        clock.set(1_000)


def test_advance_adds_milliseconds() -> None:
    clock = ManualClock(1_000)
    clock.advance(500)
    assert clock.now_ms() == 1_500


def test_advance_rejects_negative_amount() -> None:
    clock = ManualClock(1_000)
    with pytest.raises(ValueError):
        clock.advance(-1)
