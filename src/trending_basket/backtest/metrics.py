"""Daily (365-day annualization) statistics and additive PnL attribution."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from trending_basket.backtest.engine import BacktestResult

DAY_MS = 86_400_000


def calculate_metrics(result: BacktestResult) -> dict[str, Any]:
    frame = pd.DataFrame(result.equity)
    frame.index = pd.to_datetime(frame["time_ms"], unit="ms", utc=True)
    # Bar endpoints are UTC boundaries; include the initial capital observation.
    daily = frame["equity_usd"].resample("1D", closed="right", label="right").last().dropna()
    returns = daily.pct_change().dropna()
    first, last = result.equity[0], result.equity[-1]
    years = (last["time_ms"] - first["time_ms"]) / DAY_MS / 365
    total = last["equity_usd"] / first["equity_usd"] - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else None
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    vol = std * math.sqrt(365)
    downside = math.sqrt(float(returns.clip(upper=0).pow(2).mean()))
    drawdown = frame["equity_usd"] / frame["equity_usd"].cummax() - 1
    duration_ms, peak_ms = 0, first["time_ms"]
    underwater = False
    for row, dd in zip(result.equity, drawdown, strict=True):
        if dd >= -1e-12:
            if underwater:
                duration_ms = max(duration_ms, row["time_ms"] - peak_ms)
            underwater = False
            peak_ms = row["time_ms"]
        else:
            underwater = True
            duration_ms = max(duration_ms, row["time_ms"] - peak_ms)
    closed = [p for p in result.positions if p["exit_time_ms"] is not None]
    winners = [p for p in closed if p["net_pnl_usd"] > 0]
    losers = [p for p in closed if p["net_pnl_usd"] < 0]
    funding = [e for e in result.events if e["kind"] == "funding"]
    observed = sum(e["observed"] for e in funding)
    coverage = observed / len(funding) if funding else None
    warnings = []
    if coverage is not None and coverage < 0.99:
        warnings.append("Funding coverage below 99%; absent rates are zero.")
    if any(e["kind"] == "unknown_min_notional" for e in result.events):
        warnings.append(
            "Some Closed instruments have no minNotionalValue; only quantity minima apply."
        )
    if min(frame["equity_usd"]) <= 0:
        warnings.append(
            "Nonpositive equity: margin/liquidations are not modelled; ratios are invalid."
        )
    gross_profit = sum(max(0.0, p["gross_pnl_usd"]) for p in result.positions)
    turnover = sum(f["quantity"] * f["price"] for f in result.fills)
    exposures = frame.iloc[1:]

    def mean_r(positions: list[dict[str, Any]]) -> float | None:
        values = [p["return_r"] for p in positions if p["return_r"] is not None]
        return sum(values) / len(values) if values else None

    by_symbol: dict[str, dict[str, float | int]] = {}
    for p in result.positions:
        row = by_symbol.setdefault(
            p["symbol"],
            {
                "positions": 0,
                "gross_pnl_usd": 0.0,
                "net_pnl_usd": 0.0,
                "fees_usd": 0.0,
                "slippage_usd": 0.0,
                "funding_usd": 0.0,
            },
        )
        row["positions"] += 1
        for key in ("gross_pnl_usd", "net_pnl_usd", "fees_usd", "slippage_usd", "funding_usd"):
            row[key] += p[key]
    by_year: dict[str, dict[str, Any]] = {}
    previous = first
    for row in result.equity[1:]:
        # The 1 January endpoint belongs to the bar ending in the preceding year.
        year = str(pd.Timestamp(row["time_ms"] - 1, unit="ms", tz="UTC").year)
        annual = by_year.setdefault(
            year,
            {
                "start_equity_usd": previous["equity_usd"],
                "end_equity_usd": row["equity_usd"],
                "fees_usd": 0.0,
                "slippage_usd": 0.0,
                "funding_usd": 0.0,
            },
        )
        annual["end_equity_usd"] = row["equity_usd"]
        for key in ("fees_usd", "slippage_usd", "funding_usd"):
            annual[key] += row[key] - previous[key]
        previous = row
    for row_year in by_year.values():
        row_year["net_pnl_usd"] = row_year["end_equity_usd"] - row_year["start_equity_usd"]
        row_year["return_frac"] = (
            row_year["end_equity_usd"] / row_year["start_equity_usd"] - 1
            if row_year["start_equity_usd"] > 0
            else None
        )
    metrics: dict[str, Any] = {
        "initial_capital_usd": first["equity_usd"],
        "final_equity_usd": last["equity_usd"],
        "total_return_frac": total,
        "cagr_frac": cagr,
        "annual_volatility_frac": vol,
        "sharpe": float(returns.mean()) / std * math.sqrt(365) if std > 0 else None,
        "sortino": float(returns.mean()) / downside * math.sqrt(365) if downside > 0 else None,
        "max_drawdown_frac": float(drawdown.min()),
        "drawdown_duration_days": duration_ms / DAY_MS,
        "calmar": cagr / abs(float(drawdown.min()))
        if cagr is not None and drawdown.min() < 0
        else None,
        "annual_turnover": turnover / float(daily.mean()) / years if daily.mean() > 0 else None,
        "gross_profit_usd": gross_profit,
        "closed_positions": len(closed),
        "open_positions": len(result.positions) - len(closed),
        "win_rate_frac": len(winners) / len(closed) if closed else None,
        "average_win_r": mean_r(winners),
        "average_loss_r": mean_r(losers),
        "funding_coverage_frac": coverage,
        "funding_observed": observed,
        "funding_expected": len(funding),
        "funding_price_sources": {
            source: sum(e["price_source"] == source for e in funding)
            for source in sorted({e["price_source"] for e in funding})
        },
        "minimum_order_skips": sum(e["kind"] == "minimum_order_skip" for e in result.events),
        "delisting_exits": sum(p["exit_reason"] == "delisting" for p in closed),
        "btc_price_return_frac": result.btc_price_return,
        "by_year": by_year,
        "by_symbol": dict(sorted(by_symbol.items())),
        "warnings": warnings,
    }
    for kind in ("gross", "net"):
        values = exposures[f"{kind}_exposure"]
        metrics[f"average_{kind}_exposure"] = float(values.mean())
        metrics[f"max_{kind}_exposure"] = float(values.max())
    metrics["max_abs_net_exposure"] = float(exposures["net_exposure"].abs().max())
    for key in ("fees_usd", "slippage_usd", "funding_usd"):
        metrics[key] = last[key]
        metrics[key.replace("_usd", "_pct_gross_profit")] = (
            last[key] / gross_profit * 100 if gross_profit else None
        )
    if min(frame["equity_usd"]) <= 0:
        for key in ("cagr_frac", "sharpe", "sortino", "calmar", "annual_volatility_frac"):
            metrics[key] = None
    return metrics
