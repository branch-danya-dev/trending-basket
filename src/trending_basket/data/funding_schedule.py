"""Infer settlement regimes and missing timestamps from observed history.

Repeated adjacent differences establish a regime. A longer multiple bracketed
by the same regime is a data gap; a bridge between different regimes is a
transition. No present-day instrument interval is used. This retrospective
data-quality inference is never a strategy input; absent rates remain zero.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any


@dataclass(frozen=True)
class FundingGap:
    start_ms: int
    end_ms: int  # Last missing settlement, inclusive.
    interval_ms: int
    source: str

    def timestamps(self) -> range:
        return range(self.start_ms, self.end_ms + 1, self.interval_ms)


class FundingSchedule:
    def __init__(self, timestamps: Iterable[int], start_ms: int, end_ms: int) -> None:
        self.observed = tuple(sorted(set(timestamps)))
        self.known = len(self.observed) >= 2
        self.gaps: list[FundingGap] = []
        self.regimes: list[dict[str, int]] = []
        self.transitions: list[dict[str, int]] = []
        times = self.observed
        diffs = [b - a for a, b in pairwise(times)]
        runs: list[tuple[int, int, int]] = []
        for i, period in enumerate(diffs):
            if runs and runs[-1][2] == period:
                first, _, _ = runs[-1]
                runs[-1] = first, i + 1, period
            else:
                runs.append((i, i + 1, period))
        stable = [r for r in runs if r[1] - r[0] >= 2]
        for first, stop, period in runs:
            before = next((r[2] for r in reversed(stable) if r[1] <= first), None)
            after = next((r[2] for r in stable if r[0] >= stop), None)
            if (
                stop - first == 1
                and before == after
                and before is not None
                and period > before
                and period % before == 0
            ):
                self._gap(
                    times[first] + before,
                    times[stop] - before,
                    before,
                    "internal",
                    start_ms,
                    end_ms,
                )
            elif (
                stop - first == 1
                and before is not None
                and after is not None
                and before != after
                and period <= max(before, after)
            ):
                self.transitions.append(
                    {
                        "start_ms": times[first],
                        "end_ms": times[stop],
                        "before_interval_ms": before,
                        "after_interval_ms": after,
                    }
                )
            else:
                self.regimes.append(
                    {
                        "start_ms": times[first],
                        "end_ms": times[stop],
                        "interval_ms": period,
                        "observed_steps": stop - first,
                    }
                )
        if self.known:
            first_period = self.regimes[0]["interval_ms"]
            last_period = self.regimes[-1]["interval_ms"]
            if start_ms < times[0]:
                first_missing = times[0] - ((times[0] - start_ms) // first_period) * first_period
                self._gap(
                    first_missing,
                    times[0] - first_period,
                    first_period,
                    "leading_extrapolation",
                    start_ms,
                    end_ms,
                )
            self._gap(
                times[-1] + last_period,
                end_ms - 1,
                last_period,
                "trailing_extrapolation",
                start_ms,
                end_ms,
            )
        self.gaps.sort(key=lambda g: g.start_ms)
        self.missing = {t: gap for gap in self.gaps for t in gap.timestamps()}
        self.expected = tuple(
            sorted(set(t for t in times if start_ms <= t < end_ms) | self.missing.keys())
        )
        self._expected_set = set(self.expected)

    def _gap(self, first: int, last: int, period: int, source: str, start: int, end: int) -> None:
        first += max(0, (start - first + period - 1) // period) * period
        last = min(last, end - 1)
        if first <= last:
            last = first + (last - first) // period * period
            self.gaps.append(FundingGap(first, last, period, source))

    def contains(self, time_ms: int) -> bool:
        return time_ms in self._expected_set

    def between(self, start_ms: int, end_ms: int) -> tuple[int, ...]:
        return self.expected[
            bisect_left(self.expected, start_ms) : bisect_left(self.expected, end_ms)
        ]

    def metadata(self) -> dict[str, Any]:
        return {
            "interval_known": self.known,
            "history_first_ms": self.observed[0] if self.observed else None,
            "history_last_ms": self.observed[-1] if self.observed else None,
            "intervals_ms": sorted({r["interval_ms"] for r in self.regimes}),
            "regimes": self.regimes,
            "transitions": self.transitions,
        }
