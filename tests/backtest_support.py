"""Small deterministic market and universe builders for engine tests."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from trending_basket.backtest.config import Costs, Experiment, RunConfig
from trending_basket.backtest.market import DataStore
from trending_basket.backtest.sim_executor import InstrumentRules
from trending_basket.domain.types import Candle, Interval
from trending_basket.portfolio.limits import PortfolioLimits
from trending_basket.strategies.base import Decision, DecisionContext, TargetPosition
from trending_basket.strategies.benchmarks import hold_positions
from trending_basket.universe.lifecycle import TradingPeriod
from trending_basket.universe.storage import Universe

DAY = Interval.D1.duration_ms
H4 = Interval.H4.duration_ms
START = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
ZERO = Costs(
    taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0, stop_slippage_bps=0, delist_slippage_bps=0
)
FINE = InstrumentRules(Decimal("0.000000001"), Decimal("0"), Decimal("0"), 2 * H4)


def config(days=4, start=START, costs=ZERO, **limit_values):
    date = datetime.fromtimestamp(start / 1000, UTC).date()
    return Experiment(
        run=RunConfig(
            name="test",
            strategy="buy_and_hold_btc",
            universe="test",
            start=date,
            end=date + timedelta(days=days - 1),
        ),
        costs=costs,
        limits=PortfolioLimits(
            max_symbol_exposure=limit_values.pop("max_symbol_exposure", 1), **limit_values
        ),
    )


def candle(day, open=100, high=None, low=None, close=None, symbol="BTCUSDT", interval=Interval.D1):
    close = open if close is None else close
    return Candle(
        symbol,
        interval,
        START + day * DAY,
        open,
        max(open, close) if high is None else high,
        min(open, close) if low is None else low,
        close,
        100,
        10000,
    )


def universe(symbols=("BTCUSDT",), schedule=None, until=None):
    schedule = schedule or {START: list(symbols)}
    table = pd.DataFrame(
        [
            {"rebalance_time_ms": at, "symbol": s, "rank": rank}
            for at, members in schedule.items()
            for rank, s in enumerate(members, 1)
        ],
        columns=["rebalance_time_ms", "symbol", "rank"],
    )
    return Universe(
        table,
        {
            "schema_version": 2,
            "rebalance_times_ms": sorted(schedule),
            "trading_periods": {
                s: asdict(
                    TradingPeriod(
                        START - DAY,
                        (until or {}).get(s),
                        "snapshot_delivery_time",
                        "Closed" if s in (until or {}) else "Trading",
                    )
                )
                for s in symbols
            },
        },
    )


def store(rows, extra=()):
    grouped = {}
    for row in [*rows, *extra]:
        grouped.setdefault((row.symbol, row.interval), []).append(row)
    return DataStore(grouped)


class EnterOnce:
    name = "test_entry"

    def __init__(self, amount=404, stop=90, risk=40, symbol="BTCUSDT"):
        self.amount, self.stop, self.risk, self.symbol = amount, stop, risk, symbol
        self.entered = False

    def decide(self, ctx: DecisionContext) -> Decision:
        if self.entered:
            return hold_positions(ctx)
        if not ctx.market.candles(self.symbol, Interval.D1, 1):
            return {}
        self.entered = True
        return {self.symbol: TargetPosition(self.amount, self.stop, self.risk)}
