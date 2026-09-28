"""Candidate filtering on current instrument metadata and explicit exclusions."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from universe_test_support import instrument, seed_universe_inputs

from trending_basket.universe.candidates import candidate_pool, load_exclusions


def test_candidate_filters_and_asset_classification(tmp_path: Path) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    exclusions.write_text(
        "symbol_or_base,reason\nUSDC,stablecoin\nBLOCKUSDT,manual exclusion\nPAXG,gold\n",
        encoding="utf-8",
    )
    rows = [
        instrument("BTCUSDT"),
        instrument("INNOVUSDT", symbol_type="innovation"),
        instrument("FUTUREUSDT", contract_type="LinearFutures"),
        instrument("BTCUSDC", quote_coin="USDC"),
        instrument("CLOSEDUSDT", status="Closed"),
        instrument("CLOSEDGOLDUSDT", status="Closed", symbol_type="commodity"),
        instrument("USDCUSDT", status="Closed"),
        instrument("PREUSDT", status="PreLaunch"),
        instrument("BLOCKUSDT"),
        instrument("PAXGUSDT"),
        instrument("AAPLUSDT", symbol_type="stock"),
        instrument("SPYUSDT", symbol_type="ETF"),
        instrument("XAUUSDT", symbol_type="commodity"),
        instrument("EURUSDT", symbol_type="forex"),
        instrument("TOKENUSDT", symbol_type="xstocks"),
        instrument("INDEXUSDT", symbol_type="index"),
        instrument("FUTURETYPEUSDT", symbol_type="new-class"),
        instrument("REGIONUSDT", market_region="US"),
        instrument("TICKERUSDT", underlying_ticker="AAPL"),
        instrument("UNKNOWNUSDT", symbol_type=None),
    ]
    pd.DataFrame(rows).to_parquet(
        tmp_path / "bybit/linear/instruments/2024-06-02.parquet", index=False
    )
    pool = candidate_pool(tmp_path, exclusions)
    assert pool.symbols == ["BTCUSDT", "CLOSEDUSDT", "INNOVUSDT"]
    assert pool.snapshot.name == "2024-06-02.parquet"
    assert pool.snapshot_status_counts["Closed"] == 3
    assert pool.excluded["USDCUSDT"] == "stablecoin"
    assert pool.excluded["BLOCKUSDT"] == "manual exclusion"


def test_old_snapshot_requires_resync(tmp_path: Path) -> None:
    exclusions = seed_universe_inputs(tmp_path)
    path = tmp_path / "bybit/linear/instruments/2024-06-01.parquet"
    pd.read_parquet(path).drop(columns=["symbol_type"]).to_parquet(path, index=False)
    with pytest.raises(ValueError, match="sync instruments"):
        candidate_pool(tmp_path, exclusions)


def test_missing_snapshot_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sync instruments"):
        candidate_pool(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "symbol,reason\nBTC,foo\n",
        "symbol_or_base,reason\nBTC,\n",
        "symbol_or_base,reason\nBTC,a\nbtc,b\n",
    ],
)
def test_invalid_exclusions_are_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "exclusions.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_exclusions(path)
