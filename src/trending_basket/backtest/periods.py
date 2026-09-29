"""Preregistered UTC periods and explicit, audited access to protected samples."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from trending_basket.clock import Clock

Period = Literal["dev", "val", "holdout", "full"]
BENCHMARKS = frozenset({"buy_and_hold_btc", "equal_weight_universe"})
WARMUP_MS = 1609459200000  # 2021-01-01 UTC


def period_dates(period: Period, clock: Clock | None = None) -> tuple[date, date]:
    if period == "dev":
        return date(2021, 11, 1), date(2024, 6, 30)
    if period == "val":
        return date(2024, 7, 1), date(2025, 9, 30)
    if period not in {"holdout", "full"}:
        raise ValueError(f"unknown period: {period}")
    if clock is None:
        raise ValueError("holdout/full resolution requires an explicit Clock")
    end = datetime.fromtimestamp(clock.now_ms() / 1000, UTC).date() - timedelta(days=1)
    start = date(2025, 10, 1) if period == "holdout" else date(2021, 11, 1)
    if end < start:
        raise ValueError("period has no closed dates")
    return start, end


@dataclass(frozen=True)
class PeriodAccess:
    experiment: str
    strategy: str
    period: Period


def authorize_period(
    *,
    experiment: str,
    strategy: str,
    period: Period,
    allow_val: bool,
    allow_holdout: bool,
    confirmed: bool,
    reports_dir: Path,
    clock: Clock,
    commit: str,
    override_holdout_lock: bool = False,
) -> PeriodAccess:
    if strategy not in BENCHMARKS and period != "dev":
        if period == "val" and not allow_val:
            raise ValueError("val requires --allow-val")
        if period in {"holdout", "full"} and not (allow_holdout and confirmed):
            raise ValueError("holdout/full requires --allow-holdout and console confirmation")
        reports_dir.mkdir(parents=True, exist_ok=True)
        entry = dict(
            time_ms=clock.now_ms(),
            experiment=experiment,
            strategy=strategy,
            commit=commit,
            period=period,
        )
        # Serialize admission, including concurrent CLI processes. A stale lock fails closed.
        lock = reports_dir / ".period-access.lock"
        stream_lock = lock.open("x", encoding="utf-8")
        try:
            with stream_lock:
                journal = reports_dir / "period-access-log.jsonl"
                previous = []
                if journal.exists():
                    for line in journal.read_text(encoding="utf-8").splitlines():
                        row = json.loads(line)
                        if not isinstance(row, dict) or not {"strategy", "period"} <= row.keys():
                            raise ValueError("invalid period access journal; refusing admission")
                        previous.append(row)
                protected = period in {"holdout", "full"}
                used = any(
                    row["strategy"] not in BENCHMARKS and row["period"] in {"holdout", "full"}
                    for row in previous
                )
                if protected and used and not override_holdout_lock:
                    raise ValueError("holdout already accessed; requires --override-holdout-lock")
                if protected and override_holdout_lock:
                    entry["warning"] = "HOLDOUT LOCK OVERRIDDEN: repeat access invalidates T005"
                with journal.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry, sort_keys=True) + "\n")
        finally:
            lock.unlink()
    return PeriodAccess(experiment, strategy, period)


def require_access(
    experiment: str, strategy: str, period: Period | None, access: PeriodAccess | None
) -> None:
    # None is reserved for in-memory synthetic engine fixtures, never accepted in TOML.
    if (
        strategy not in BENCHMARKS
        and period not in {None, "dev"}
        and access != PeriodAccess(experiment, strategy, period)
    ):
        raise ValueError("protected period requires audited authorization")
