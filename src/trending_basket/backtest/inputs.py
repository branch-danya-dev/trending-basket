"""Load cached inputs and record their ranges and hashes for an experiment."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from trending_basket.backtest.config import Experiment
from trending_basket.backtest.market import DataStore
from trending_basket.backtest.sim_executor import InstrumentRules
from trending_basket.data.cache import funding_path, klines_path
from trending_basket.domain.types import Candle, Interval
from trending_basket.universe.candidates import latest_snapshot
from trending_basket.universe.storage import Universe, load_universe, universe_paths


@dataclass
class Inputs:
    data: DataStore
    universe: Universe
    rules: dict[str, InstrumentRules]
    funding: dict[str, dict[int, float]]
    provenance: list[dict[str, Any]]


def load_inputs(data_dir: Path, experiment: Experiment) -> Inputs:
    universe = load_universe(data_dir, experiment.run.universe)
    symbols = sorted(set(universe.table["symbol"]))
    if experiment.run.strategy == "buy_and_hold_btc":
        symbols = ["BTCUSDT"]
    snapshot = latest_snapshot(data_dir)
    instruments = pd.read_parquet(snapshot).set_index("symbol")
    rules: dict[str, InstrumentRules] = {}
    funding: dict[str, dict[int, float]] = {}
    candles: dict[tuple[str, Interval], tuple[Candle, ...]] = {}
    provenance: list[dict[str, Any]] = []

    def record(path: Path, frame: pd.DataFrame | None = None, time_column: str = "") -> None:
        provenance.append(
            {
                "file": path.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": len(frame) if frame is not None else None,
                "first_time_ms": int(frame[time_column].min())
                if frame is not None and not frame.empty and time_column
                else None,
                "last_time_ms": int(frame[time_column].max())
                if frame is not None and not frame.empty and time_column
                else None,
            }
        )

    record(snapshot, instruments)
    for path in universe_paths(data_dir, experiment.run.universe):
        record(path)
    for symbol in symbols:
        if symbol not in instruments.index:
            raise ValueError(f"instrument missing from snapshot: {symbol}")
        row = instruments.loc[symbol]
        notional = row["min_notional_value"]
        rules[symbol] = InstrumentRules(
            Decimal(str(row["qty_step"])),
            Decimal(str(row["min_order_qty"])),
            None if pd.isna(notional) else Decimal(str(notional)),
            int(str(row["funding_interval_ms"])),
        )
        for interval in Interval:
            path = klines_path(data_dir, interval, symbol)
            if not path.is_file():
                if interval == experiment.run.interval:
                    raise ValueError(f"missing candle cache: {path}; run tb data sync klines")
                provenance.append({"file": path.as_posix(), "missing": True})
                continue
            frame = pd.read_parquet(path)
            record(path, frame, "open_time_ms")
            frame = frame.loc[frame["open_time_ms"] < experiment.run.end_ms]
            rows = []
            for r in frame.to_dict(orient="records"):
                values = [
                    float(r[k]) for k in ("open", "high", "low", "close", "volume", "turnover")
                ]
                if not all(math.isfinite(v) for v in values):
                    raise ValueError(f"nonfinite candle in {path}")
                rows.append(Candle(symbol, interval, int(r["open_time_ms"]), *values))
            candles[symbol, interval] = tuple(rows)
        path = funding_path(data_dir, symbol)
        funding[symbol] = {}
        if not path.is_file():
            provenance.append({"file": path.as_posix(), "missing": True})
            continue
        frame = pd.read_parquet(path)
        record(path, frame, "funding_time_ms")
        if (
            frame["funding_time_ms"].duplicated().any()
            or not frame["rate_frac"].map(math.isfinite).all()
        ):
            raise ValueError(f"invalid funding cache: {path}")
        funding[symbol] = {
            int(r["funding_time_ms"]): float(r["rate_frac"])
            for r in frame.to_dict(orient="records")
        }
    return Inputs(DataStore(candles), universe, rules, funding, provenance)
