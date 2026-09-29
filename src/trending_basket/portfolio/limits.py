"""Pure proportional exposure limits, shared by simulation and future live execution."""

from __future__ import annotations

from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field

from trending_basket.strategies.base import Decision


class PortfolioLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    max_gross_exposure: float = Field(default=2.0, gt=0)
    max_net_exposure: float = Field(default=1.5, gt=0)
    max_symbol_exposure: float = Field(default=0.5, gt=0)
    exit_on_universe_removal: bool = False


def apply_limits(
    decision: Decision, equity_usd: float, limits: PortfolioLimits
) -> tuple[Decision, list[str]]:
    if equity_usd <= 0:
        return {s: replace(t, notional_usd=0) for s, t in decision.items()}, ["capital_depleted"]
    notionals = [t.notional_usd for t in decision.values()]
    measures = {
        "gross_exposure": (sum(abs(n) for n in notionals), limits.max_gross_exposure),
        "net_exposure": (abs(sum(notionals)), limits.max_net_exposure),
        "symbol_exposure": (
            max((abs(n) for n in notionals), default=0),
            limits.max_symbol_exposure,
        ),
    }
    exceeded = [name for name, (value, cap) in measures.items() if value > equity_usd * cap]
    factor = min(
        [1.0] + [equity_usd * cap / value for value, cap in measures.values() if value > 0]
    )
    return {
        s: replace(t, notional_usd=t.notional_usd * factor) for s, t in decision.items()
    }, exceeded
