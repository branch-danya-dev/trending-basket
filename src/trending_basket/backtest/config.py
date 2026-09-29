"""Strict, versioned-by-Git TOML experiments; no implicit current-time end date."""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trending_basket.backtest.periods import Period, period_dates
from trending_basket.clock import Clock
from trending_basket.domain.types import Interval
from trending_basket.portfolio.limits import PortfolioLimits
from trending_basket.strategies.trend_basket import TrendBasketParams


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    taker_fee_bps: float = Field(default=5.5, ge=0, lt=10000)
    maker_fee_bps: float = Field(default=2.0, ge=0, lt=10000)
    slippage_bps: float = Field(default=2.0, ge=0, lt=10000)
    stop_slippage_bps: float = Field(default=5.0, ge=0, lt=10000)
    delist_slippage_bps: float = Field(default=200.0, ge=0, lt=10000)
    rebalance_fill: Literal["taker", "maker"] = "taker"


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    strategy: Literal["buy_and_hold_btc", "equal_weight_universe", "trend_basket"]
    universe: str
    period: Period | None = None
    symbols: tuple[str, ...] | None = None
    interval: Interval = Interval.D1
    start: date
    end: date
    initial_capital_usd: float = Field(default=1000, gt=0)

    @model_validator(mode="after")
    def ordered_dates(self) -> RunConfig:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        return self

    @property
    def start_ms(self) -> int:
        return int(datetime.combine(self.start, datetime.min.time(), UTC).timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(
            datetime.combine(self.end + timedelta(days=1), datetime.min.time(), UTC).timestamp()
            * 1000
        )


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run: RunConfig
    costs: Costs = Costs()
    limits: PortfolioLimits = PortfolioLimits()
    strategy_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def no_unused_parameters(self) -> Experiment:
        if self.run.strategy == "trend_basket":
            TrendBasketParams.model_validate(self.strategy_params)
            if not self.limits.enabled:
                raise ValueError("trend strategy requires enabled exposure limits")
        elif self.strategy_params:
            raise ValueError("reference strategies have no tunable strategy_params")
        return self


def load_experiment(path: Path, clock: Clock | None = None) -> Experiment:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    run = raw.get("run", {})
    if "period" not in run or "start" in run or "end" in run:
        raise ValueError("run.period is mandatory; manual start/end are forbidden")
    run["start"], run["end"] = period_dates(run["period"], clock)
    return Experiment.model_validate(raw)
