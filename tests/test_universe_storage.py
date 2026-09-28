"""Reproducibility, provenance, empty months and atomic universe publication."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from universe_test_support import JUNE_MS, MAY_MS, seed_universe_inputs

from trending_basket.clock import ManualClock
from trending_basket.universe import storage
from trending_basket.universe.selection import SelectionParameters
from trending_basket.universe.storage import (
    build_universe,
    load_universe,
    save_universe,
    universe_paths,
)


def test_build_metadata_query_boundaries_and_idempotent_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    monkeypatch.setattr(storage, "_git_commit", lambda: "abc123")
    kwargs = dict(
        data_dir=tmp_path,
        name="core15",
        since_ms=MAY_MS,
        parameters=SelectionParameters(),
        clock=ManualClock(JUNE_MS),
        exclusions_path=exclusions,
    )
    universe = build_universe(**kwargs)
    parquet, meta_path = universe_paths(tmp_path, "core15")
    before = parquet.read_bytes()
    build_universe(**kwargs)
    assert parquet.read_bytes() == before
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["parameters"] == {
        "top_n": 15,
        "min_history_days": 120,
        "turnover_window_days": 30,
        "min_median_turnover_usd": 10_000_000,
    }
    assert meta["created_time_ms"] == JUNE_MS
    assert meta["git_commit"] == "abc123"
    assert meta["instrument_snapshot"] == "2024-06-01.parquet"
    assert meta["input_candles"]["BTCUSDT"]["rows"] == 120
    assert meta["input_candles"]["BTCUSDT"]["last_open_time_ms"] == MAY_MS - 86_400_000
    assert meta["survivorship_bias"]["delisted_included"] == 0
    assert meta["survivorship_bias"]["delisted_without_history"] == 0
    assert meta["snapshot_contains_closed"] is False
    assert meta["missing_caches"] == ["MISSINGUSDT"]
    assert meta["underfilled_months_ms"] == [MAY_MS, JUNE_MS]
    assert universe.universe_at(MAY_MS - 1) == []
    assert universe.universe_at(MAY_MS) == ["BTCUSDT", "ETHUSDT"]
    assert universe.universe_at(JUNE_MS - 1) == ["BTCUSDT", "ETHUSDT"]
    assert universe.universe_at(JUNE_MS) == []
    assert universe.universe_at(JUNE_MS + 10_000) == []
    pd.testing.assert_frame_equal(load_universe(tmp_path, "core15").table, universe.table)
    assert not list((tmp_path / "universe").glob("*.tmp"))


def test_failed_serialization_leaves_old_artifact_unchanged(tmp_path: Path) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    universe = build_universe(
        data_dir=tmp_path,
        name="core15",
        since_ms=MAY_MS,
        parameters=SelectionParameters(),
        clock=ManualClock(JUNE_MS),
        exclusions_path=exclusions,
    )
    parquet, meta = universe_paths(tmp_path, "core15")
    previous = (parquet.read_bytes(), meta.read_bytes())
    universe.metadata["unserializable"] = object()
    with pytest.raises(TypeError):
        save_universe(tmp_path, "core15", universe)
    assert previous == (parquet.read_bytes(), meta.read_bytes())
    assert not list((tmp_path / "universe").glob("*.tmp"))


def test_interrupted_pair_publication_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    universe = build_universe(
        data_dir=tmp_path,
        name="core15",
        since_ms=MAY_MS,
        parameters=SelectionParameters(),
        clock=ManualClock(JUNE_MS),
        exclusions_path=exclusions,
    )
    universe.table.loc[0, "median_turnover_usd"] += 1
    replace = storage.os.replace
    calls: list[Path] = []

    def interrupt(source: Path, target: Path) -> None:
        assert source.is_file()
        calls.append(target)
        if len(calls) == 2:
            raise OSError("simulated interruption")
        replace(source, target)

    monkeypatch.setattr(storage.os, "replace", interrupt)
    with pytest.raises(OSError):
        save_universe(tmp_path, "core15", universe)
    with pytest.raises(ValueError, match="do not match"):
        load_universe(tmp_path, "core15")
    assert not list((tmp_path / "universe").glob("*.tmp"))


def test_all_missing_caches_still_write_empty_months(tmp_path: Path) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    for path in (tmp_path / "bybit/linear/klines/1d").glob("*.parquet"):
        path.unlink()
    result = build_universe(
        data_dir=tmp_path,
        name="empty",
        since_ms=MAY_MS,
        parameters=SelectionParameters(),
        clock=ManualClock(JUNE_MS),
        exclusions_path=exclusions,
    )
    assert result.table.empty
    assert len(result.metadata["missing_caches"]) == 3
    assert load_universe(tmp_path, "empty").universe_at(JUNE_MS) == []


@pytest.mark.parametrize("name", ["../escape", "folder/name", "C:\\escape", ""])
def test_unsafe_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        universe_paths(tmp_path, name)


def test_old_universe_requires_rebuild_for_trading_bounds(tmp_path: Path) -> None:
    _, meta = universe_paths(tmp_path, "old")
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="rebuild"):
        load_universe(tmp_path, "old")
