"""Current candidate pool, with explicit asset-class and exclusion rules."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from trending_basket.data.cache import instruments_dir

DEFAULT_EXCLUSIONS = Path("config/universe_exclusions.csv")
CRYPTO_SYMBOL_TYPES = frozenset({"", "innovation"})
CLASSIFICATION_RULE = (
    "Trading LinearPerpetual USDT; symbol_type in ['', 'innovation']; "
    "empty market_region and underlying_ticker; explicit symbol/base exclusions"
)


@dataclass(frozen=True)
class CandidatePool:
    snapshot: Path
    symbols: list[str]
    excluded: dict[str, str]
    snapshot_status_counts: dict[str, int]
    exclusions: dict[str, str]


def load_exclusions(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["symbol_or_base", "reason"]:
            raise ValueError("exclusions CSV must have columns symbol_or_base,reason")
        result: dict[str, str] = {}
        for row in reader:
            key = (row.get("symbol_or_base") or "").strip().upper()
            reason = (row.get("reason") or "").strip()
            if not key or not reason or key in result:
                raise ValueError("exclusions must have unique nonempty keys and reasons")
            result[key] = reason
    return result


def latest_snapshot(data_dir: Path) -> Path:
    snapshots = []
    for path in instruments_dir(data_dir).glob("*.parquet"):
        try:
            date.fromisoformat(path.stem)
        except ValueError:
            continue
        snapshots.append(path)
    if not snapshots:
        raise ValueError("no instrument snapshot; run tb data sync instruments first")
    return max(snapshots, key=lambda p: p.stem)


def candidate_pool(data_dir: Path, exclusions_path: Path = DEFAULT_EXCLUSIONS) -> CandidatePool:
    snapshot = latest_snapshot(data_dir)
    instruments = pd.read_parquet(snapshot)
    required = {
        "symbol",
        "base_coin",
        "quote_coin",
        "contract_type",
        "status",
        "symbol_type",
        "market_region",
        "underlying_ticker",
    }
    if not required.issubset(instruments.columns):
        raise ValueError("snapshot lacks classification fields; run tb data sync instruments again")
    if instruments["symbol"].duplicated().any():
        raise ValueError("instrument snapshot contains duplicate symbols")
    exclusions = load_exclusions(exclusions_path)
    selected: list[str] = []
    excluded: dict[str, str] = {}
    for row in instruments.to_dict(orient="records"):
        symbol = str(row["symbol"])
        reason = ""
        if row["contract_type"] != "LinearPerpetual" or row["quote_coin"] != "USDT":
            reason = "not a linear USDT perpetual"
        elif row["status"] != "Trading":
            reason = "not Trading"
        elif symbol in exclusions or str(row["base_coin"]) in exclusions:
            reason = exclusions.get(symbol, exclusions.get(str(row["base_coin"]), ""))
        elif any(
            pd.isna(row[key]) for key in ("symbol_type", "market_region", "underlying_ticker")
        ):
            reason = "missing asset classification"
        elif row["symbol_type"] not in CRYPTO_SYMBOL_TYPES:
            reason = f"non-crypto or unknown symbol_type: {row['symbol_type']}"
        elif row["market_region"] or row["underlying_ticker"]:
            reason = "traditional underlying asset"
        if reason:
            excluded[symbol] = reason
        else:
            selected.append(symbol)
    counts = {str(key): int(value) for key, value in instruments["status"].value_counts().items()}
    return CandidatePool(snapshot, sorted(selected), excluded, counts, exclusions)
