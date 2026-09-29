"""Pure proportional exposure limits, shared by simulation and future live execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field

from trending_basket.strategies.base import Decision


class PortfolioLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    enabled: bool = True
    max_gross_exposure: float = Field(default=2.0, gt=0)
    max_net_exposure: float = Field(default=1.5, gt=0)
    max_symbol_exposure: float = Field(default=0.5, gt=0)
    exit_on_universe_removal: bool = False


def actual_exposures(notionals: Mapping[str, float], equity_usd: float) -> dict[str, float]:
    """Exposures of executed quantities at one common set of market marks."""
    denominator = equity_usd if equity_usd > 0 else 1.0
    return {
        "gross_exposure": sum(abs(n) for n in notionals.values()) / denominator,
        "net_exposure": abs(sum(notionals.values())) / denominator,
        "symbol_exposure": max((abs(n) for n in notionals.values()), default=0) / denominator,
    }


def limit_violations(
    notionals: Mapping[str, float], equity_usd: float, limits: PortfolioLimits
) -> list[str]:
    if not limits.enabled:
        return []
    if equity_usd <= 0 and any(notionals.values()):
        return ["capital_depleted"]
    values = actual_exposures(notionals, equity_usd)
    return [key for key, value in values.items() if value > getattr(limits, f"max_{key}") + 1e-9]


def assert_actual_limits(
    notionals: Mapping[str, float], equity_usd: float, limits: PortfolioLimits
) -> None:
    exceeded = limit_violations(notionals, equity_usd, limits)
    if exceeded:
        raise ArithmeticError(f"post-rebalance exposure invariant failed: {exceeded}")


def apply_limits(
    decision: Decision, equity_usd: float, limits: PortfolioLimits
) -> tuple[Decision, list[str]]:
    if not limits.enabled:
        return dict(decision), []
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
        s: replace(
            t,
            notional_usd=t.notional_usd * factor,
            initial_risk_usd=t.initial_risk_usd * factor
            if t.initial_risk_usd is not None
            else None,
        )
        for s, t in decision.items()
    }, exceeded
