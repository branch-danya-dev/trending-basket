"""Build, persist and query monthly universes with reproducible input provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from trending_basket.clock import Clock
from trending_basket.data.cache import klines_path
from trending_basket.domain.types import Interval
from trending_basket.universe.candidates import (
    CLASSIFICATION_RULE,
    DEFAULT_EXCLUSIONS,
    candidate_pool,
)
from trending_basket.universe.selection import (
    SelectionParameters,
    empty_universe,
    monthly_schedule,
    select_universe,
)

SURVIVORSHIP_EXPLANATION = (
    "Candidates come from the latest Trading USDT-perpetual snapshot, not historical listings. "
    "Delisted symbols are excluded even when the API exposes them with status=Closed. "
    "Historical results have survivorship bias and may overstate performance. "
    "Only candle selection is point-in-time; historical candidate membership is not."
)


@dataclass
class Universe:
    table: pd.DataFrame
    metadata: dict[str, Any]

    def universe_at(self, time_ms: int) -> list[str]:
        """Latest scheduled composition, including empty months; before the first, []."""
        times: list[int] = self.metadata["rebalance_times_ms"]
        index = bisect_right(times, time_ms) - 1
        if index < 0:
            return []
        selected = self.table.loc[self.table["rebalance_time_ms"] == times[index]]
        return [str(symbol) for symbol in selected.sort_values("rank")["symbol"]]


def universe_paths(data_dir: Path, name: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("name must contain only letters, digits, underscores and hyphens")
    directory = data_dir / "universe"
    return directory / f"{name}.parquet", directory / f"{name}.meta.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def save_universe(data_dir: Path, name: str, universe: Universe) -> None:
    parquet_path, metadata_path = universe_paths(data_dir, name)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_temp = parquet_path.with_name(parquet_path.name + ".tmp")
    metadata_temp = metadata_path.with_name(metadata_path.name + ".tmp")
    try:
        universe.table.to_parquet(parquet_temp, index=False)
        universe.metadata["parquet_sha256"] = _sha256(parquet_temp)
        metadata_temp.write_text(
            json.dumps(
                universe.metadata, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n",
            encoding="utf-8",
        )
        # Prepare both before publishing either. Metadata is the last published commit marker.
        os.replace(parquet_temp, parquet_path)
        os.replace(metadata_temp, metadata_path)
    finally:
        parquet_temp.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)


def load_universe(data_dir: Path, name: str) -> Universe:
    parquet_path, metadata_path = universe_paths(data_dir, name)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if _sha256(parquet_path) != metadata["parquet_sha256"]:
        raise ValueError("universe data and metadata do not match; rebuild the universe")
    return Universe(pd.read_parquet(parquet_path), metadata)


def build_universe(
    *,
    data_dir: Path,
    name: str,
    since_ms: int,
    parameters: SelectionParameters,
    clock: Clock,
    exclusions_path: Path = DEFAULT_EXCLUSIONS,
) -> Universe:
    universe_paths(data_dir, name)
    now_ms = clock.now_ms()
    schedule = monthly_schedule(since_ms, now_ms)
    pool = candidate_pool(data_dir, exclusions_path)
    candles: dict[str, pd.DataFrame] = {}
    inputs: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for symbol in pool.symbols:
        path = klines_path(data_dir, Interval.D1, symbol)
        if not path.is_file():
            missing.append(symbol)
            continue
        frame = pd.read_parquet(path)
        if not {"open_time_ms", "turnover"}.issubset(frame.columns):
            raise ValueError(f"invalid candle cache: {path}")
        candles[symbol] = frame
        inputs[symbol] = {
            "file": path.relative_to(data_dir).as_posix(),
            "rows": len(frame),
            "last_open_time_ms": int(frame["open_time_ms"].max()) if not frame.empty else None,
            "sha256": _sha256(path),
        }
    selections = [select_universe(candles, at, parameters) for at in schedule]
    table = pd.concat(selections, ignore_index=True) if selections else empty_universe()
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "name": name,
        "parameters": asdict(parameters),
        "turnover_currency": "USDT",
        "usd_conversion_assumption": "USDT turnover is used as a USD proxy; no FX conversion",
        "created_time_ms": now_ms,
        "git_commit": _git_commit(),
        "instrument_snapshot": pool.snapshot.name,
        "instrument_snapshot_sha256": _sha256(pool.snapshot),
        "snapshot_status_counts": pool.snapshot_status_counts,
        "snapshot_contains_closed": pool.snapshot_status_counts.get("Closed", 0) > 0,
        "candidate_pool_source": "latest Trading snapshot",
        "candidate_symbols": pool.symbols,
        "candidate_count": len(pool.symbols),
        "excluded_candidates": pool.excluded,
        "classification_rule": CLASSIFICATION_RULE,
        "exclusions_file": str(exclusions_path),
        "exclusions_sha256": _sha256(exclusions_path),
        "exclusions": pool.exclusions,
        "input_candles": inputs,
        "missing_caches": missing,
        "survivorship_bias": True,
        "survivorship_bias_explanation": SURVIVORSHIP_EXPLANATION,
        "rebalance_times_ms": schedule,
        "underfilled_months_ms": [
            at
            for at, frame in zip(schedule, selections, strict=True)
            if len(frame) < parameters.top_n
        ],
    }
    result = Universe(table, metadata)
    save_universe(data_dir, name, result)
    return result
