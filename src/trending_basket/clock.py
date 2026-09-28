"""Time source abstraction. SystemClock is the only place system time is read."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """A source of the current time, in milliseconds since the Unix epoch."""

    def now_ms(self) -> int:
        """Return the current time as milliseconds since the Unix epoch."""
        ...


class SystemClock:
    """Wall-clock time from the operating system.

    This is the only place in the package allowed to read system time directly.
    """

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ManualClock:
    """Explicitly controlled clock, for tests and the backtest engine."""

    def __init__(self, start_ms: int) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def set(self, ms: int) -> None:
        """Set the clock to `ms`. Raises ValueError if that would move it backward."""
        if ms < self._now_ms:
            raise ValueError(f"clock cannot move backward: {ms} < {self._now_ms}")
        self._now_ms = ms

    def advance(self, ms: int) -> None:
        """Advance the clock by `ms` milliseconds. Raises ValueError if `ms` is negative."""
        if ms < 0:
            raise ValueError(f"advance amount must be non-negative, got {ms}")
        self._now_ms += ms
