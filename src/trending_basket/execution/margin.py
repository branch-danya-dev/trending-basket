"""Directional liquidation bounds and account stress, fixed by ADR-020."""

from decimal import Decimal, InvalidOperation
from typing import Any

from trending_basket.execution.planning import ExchangeRules

D = Decimal
MMR_LIMIT = D("0.30")


def number(value: Any) -> Decimal:
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("missing or invalid margin data") from None
    if not result.is_finite():
        raise ValueError("nonfinite margin data")
    return result


def liquidation_distance(position: dict[str, Any], rules: ExchangeRules) -> Decimal:
    entry = number(position["avgPrice"])
    side = position["side"]
    if side not in {"Buy", "Sell"} or entry <= 0:
        raise ValueError("invalid position for liquidation check")
    boundary: D | None
    raw = position.get("liqPrice")
    if raw not in (None, ""):
        boundary = number(raw)
        if boundary <= 0:
            raise ValueError("invalid numeric liquidation price")
    else:
        boundary = rules.min_price if side == "Buy" else rules.max_price
        if boundary is None or not boundary.is_finite() or boundary <= 0:
            raise ValueError("liquidation bound unavailable")
    distance = entry - boundary if side == "Buy" else boundary - entry
    if distance <= 0:
        raise ValueError("liquidation boundary on wrong side")
    return distance


def cross_margin_stress(
    wallet: dict[str, Any],
    positions: dict[str, dict[str, Any]],
) -> dict[str, float]:
    rate = number(wallet.get("accountMMRate"))
    mm = number(wallet.get("totalMaintenanceMargin"))
    margin = number(wallet.get("totalMarginBalance"))
    available = number(wallet.get("totalAvailableBalance"))
    initial = number(wallet.get("totalInitialMargin"))
    if min(rate, mm, initial) < 0 or margin <= 0 or rate >= MMR_LIMIT:
        raise ValueError("account MMR at or above 30 percent or invalid margin")
    bounds = [margin, available + initial]
    if rate > 0:
        if mm <= 0:
            raise ValueError("inconsistent account MMR")
        bounds.append(mm / rate)
    denominator = min(bounds)
    loss = D(0)
    stressed_mm = mm
    for p in positions.values():
        mark, stop, qty = (number(p[k]) for k in ("markPrice", "stopLoss", "size"))
        if min(mark, stop, qty) <= 0:
            raise ValueError("stop stress requires positive prices and quantity")
        sign = D(1) if p["side"] == "Buy" else D(-1)
        loss += max(D(0), sign * (mark - stop) * qty)
        loss += qty * stop * D("0.00105")  # 5.5 bps fee + 5 bps stop slippage.
        # Never credit reduced maintenance requirements before exits have filled.
        if stop > mark:
            stressed_mm += number(p.get("positionMM")) * (stop / mark - 1)
    stressed_denominator = denominator - loss
    if stressed_denominator <= 0:
        raise ValueError("stop stress exhausts account margin")
    stressed_rate = max(rate, stressed_mm / stressed_denominator)
    if stressed_rate >= MMR_LIMIT:
        raise ValueError("stop stress MMR at or above 30 percent")
    return dict(
        account_mm_rate=float(rate),
        stressed_mm_rate=float(stressed_rate),
        stop_stress_loss_usd=float(loss),
        effective_margin_usd=float(denominator),
    )
