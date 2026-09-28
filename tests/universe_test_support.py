"""Synthetic instrument snapshots and candle caches for universe tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from trending_basket.data.cache import klines_path
from trending_basket.domain.types import Interval
from trending_basket.universe.selection import DAY_MS

MAY_MS = int(datetime(2024, 5, 1, tzinfo=UTC).timestamp() * 1000)
JUNE_MS = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp() * 1000)


def instrument(symbol: str = "BTCUSDT", **overrides: str | None) -> dict[str, str | None]:
    return {
        "symbol": symbol,
        "base_coin": symbol.removesuffix("USDT"),
        "quote_coin": "USDT",
        "contract_type": "LinearPerpetual",
        "status": "Trading",
        "symbol_type": "",
        "market_region": "",
        "underlying_ticker": "",
        **overrides,
    }


def seed_universe_inputs(data_dir: Path) -> Path:
    snapshot = data_dir / "bybit/linear/instruments/2024-06-01.parquet"
    snapshot.parent.mkdir(parents=True)
    pd.DataFrame(
        [instrument(symbol) for symbol in ["BTCUSDT", "ETHUSDT", "MISSINGUSDT"]]
    ).to_parquet(snapshot, index=False)
    exclusions = data_dir / "exclusions.csv"
    exclusions.write_text("symbol_or_base,reason\nUSDC,stablecoin\n", encoding="utf-8")
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        path = klines_path(data_dir, Interval.D1, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "open_time_ms": list(range(MAY_MS - 120 * DAY_MS, MAY_MS, DAY_MS)),
                "turnover": [20_000_000.0] * 120,
            }
        ).to_parquet(path, index=False)
    return exclusions
