"""Strict, versioned-by-Git TOML experiments; no implicit current-time end date."""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trending_basket.domain.types import Interval
from trending_basket.portfolio.limits import PortfolioLimits


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
    strategy: Literal["buy_and_hold_btc", "equal_weight_universe"]
    universe: str
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
    strategy_params: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def no_unused_parameters(self) -> Experiment:
        if self.strategy_params:
            raise ValueError("reference strategies have no tunable strategy_params")
        return self


def load_experiment(path: Path) -> Experiment:
    return Experiment.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
