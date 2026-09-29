"""Decimal exchange targets and stop prices for the fixed long-only Demo candidate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from trending_basket.backtest.sim_executor import InstrumentRules

D = Decimal


@dataclass(frozen=True)
class ExchangeRules:
    quantity: InstrumentRules
    tick_size: Decimal
    max_market_qty: Decimal
    min_price: Decimal | None = None
    max_price: Decimal | None = None

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> ExchangeRules:
        if (
            row["status"] != "Trading"
            or row["contractType"] != "LinearPerpetual"
            or row["settleCoin"] != "USDT"
        ):
            raise ValueError("instrument is not a trading USDT perpetual")
        lot = row["lotSizeFilter"]
        return cls(
            InstrumentRules(
                D(lot["qtyStep"]),
                D(lot["minOrderQty"]),
                D(lot["minNotionalValue"]),
                int(row["fundingInterval"]) * 60000,
            ),
            D(row["priceFilter"]["tickSize"]),
            D(lot["maxMktOrderQty"]),
            D(row["priceFilter"]["minPrice"]) if row["priceFilter"].get("minPrice") else None,
            D(row["priceFilter"]["maxPrice"]) if row["priceFilter"].get("maxPrice") else None,
        )


def planned_delta(
    target_usd: float,
    held: Decimal,
    price: Decimal,
    rules: ExchangeRules,
    *,
    allow_increase: bool = True,
    slippage_bps: float = 2,
) -> tuple[Decimal, str]:
    if target_usd < 0 or held < 0 or price <= 0:
        raise ValueError("Demo V1x only permits long positions and positive prices")
    if target_usd == 0:
        return -held, "close"
    direction = 1 if D(str(target_usd)) > held * price else -1
    fill_price = price * (D(1) + direction * D(str(slippage_bps)) / 10000)
    step = rules.quantity.qty_step
    desired = (D(str(target_usd)) / fill_price / step).to_integral_value(rounding=ROUND_DOWN) * step
    if not allow_increase:
        desired = min(desired, held)
    if desired < held:
        requested = held - desired
        reduction = min(
            held,
            max(
                (requested / step).to_integral_value(rounding=ROUND_CEILING) * step,
                rules.quantity.minimum_quantity(float(fill_price)),
            ),
        )
        return -reduction, "reduction_minimum_adjustment" if reduction > requested else "reduce"
    delta = desired - held
    if delta and delta < rules.quantity.minimum_quantity(float(fill_price)):
        return D(0), "minimum_order_skip"
    return delta, "increase" if delta else "hold"


def protected_stop(target: float, existing: Decimal, tick_size: Decimal) -> Decimal:
    # Long stops round upward, never away from the protected position.
    return max(
        existing, (D(str(target)) / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
    )
