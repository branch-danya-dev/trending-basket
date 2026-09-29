"""Atomically publish reproducible research artifacts, using an injected clock."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd

from trending_basket import __version__
from trending_basket.backtest.config import Experiment
from trending_basket.backtest.engine import BacktestResult
from trending_basket.clock import Clock

ASSUMPTIONS = [
    "Periods preregistered in ADR-015: dev 2021-11-01..2024-06-30; "
    "val 2024-07-01..2025-09-30; holdout from 2025-10-01.",
    "Latest instrument filters include Closed records; historical filters are unavailable.",
    "Decisions see closed bars only; fills use the next open, with adverse slippage.",
    "A gap stop fills at the open; an intrabar touch fills at the stop at bar close. "
    "OHLC cannot identify the exact touch time; earlier funding/delisting events take priority.",
    "Funding at a boundary is charged to carried positions before new orders. "
    "Exact 4h open is preferred; otherwise the containing daily open is used.",
    "Funding intervals are inferred from consecutive historical timestamps per symbol. "
    "Stable regime changes and phase bridges are not gaps. Isolated longer multiples within "
    "a stable regime and extrapolated history edges are listed for API verification. "
    "Coverage measures settlements while a position is open; insufficient history yields null.",
    "Gross PnL uses reference prices; fees and slippage are subtracted once; funding is signed.",
    "Targets round toward zero; small reductions expand to an executable order or a full exit. "
    "When limits are enabled, actual exposures after fees and slippage are reduced "
    "until every limit holds within 1e-9. "
    "Each adjustment and post-rebalance exposure is logged. Limits may drift between decisions. "
    "Unknown Closed minNotionalValue is logged.",
    "End date is inclusive; terminal open positions are marked, without a fictitious liquidation.",
    "Margin and liquidations are not modelled. Survivorship bias remains despite Closed inclusion.",
    "Annual turnover is one-way traded notional / mean daily equity / elapsed years. "
    "Gross profit is the sum of positive gross lifecycle PnL, including terminal marks. "
    "Daily returns use 365 days; volatility uses sample deviation, Sortino all-day downside RMS. "
    "Exposure statistics sample bar closes; maximum drawdown uses every bar.",
]


def metric_label(key: str) -> str:
    return {
        "btc_price_return_frac": "btc_price_return_frac (BTC price only, no funding or costs)",
        "btc_funding_paid_usd": "btc_funding_paid_usd (gross funding paid on BTC)",
    }.get(key, key)


def scalar_table(metrics: dict[str, Any]) -> str:
    lines = ["| Metric | Value |", "|---|---:|"]
    for key, value in metrics.items():
        if not isinstance(value, (dict, list)):
            rendered = f"{value:.8g}" if isinstance(value, float) else str(value)
            lines.append(f"| {metric_label(key)} | {rendered} |")
    return "\n".join(lines)


def report_markdown(name: str, metrics: dict[str, Any]) -> str:
    lines = [f"# {name}", "", scalar_table(metrics), "", "## Warnings", ""]
    lines.extend(f"- {w}" for w in metrics["warnings"])
    lines += [
        "",
        "## Funding coverage by symbol",
        "",
        "Coverage refers to periods with an open position; no exposure or unknown interval = None.",
        "",
        "| Symbol | Intervals, h | Observed | Expected | Coverage | Paid, USD | Received, USD |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for symbol, values in metrics["funding_coverage_by_symbol"].items():
        periods = ", ".join(f"{p / 3600000:g}" for p in values["intervals_ms"]) or "unknown"
        coverage = values["coverage_frac"]
        rendered = f"{coverage:.6%}" if coverage is not None else "None"
        lines.append(
            f"| {symbol} | {periods} | {values['observed']} | {values['expected']} | "
            f"{rendered} | {values['paid_usd']:.8g} | {values['received_usd']:.8g} |"
        )
    lines += [
        "",
        "## Missing funding ranges",
        "",
        "Ranges list absent settlements under the inferred historical schedule, while held. "
        "API checks distinguish unavailable records from ambiguous schedule changes.",
        "",
    ]
    if not metrics["funding_gaps"]:
        lines.append("No missing settlements detected.")
    else:
        lines += [
            "| Symbol | First missing UTC | Last missing UTC | Interval, h | Missing | Source |",
            "|---|---|---|---:|---:|---|",
        ]
        for gap in metrics["funding_gaps"]:
            first = datetime.fromtimestamp(gap["start_ms"] / 1000, UTC).isoformat()
            last = datetime.fromtimestamp(gap["end_ms"] / 1000, UTC).isoformat()
            lines.append(
                f"| {gap['symbol']} | {first} | {last} | {gap['interval_ms'] / 3600000:g} | "
                f"{gap['missing_count']} | {gap['source']} |"
            )
    for title, key in (
        ("Year attribution", "by_year"),
        ("Symbol attribution", "by_symbol"),
        ("Costs by direction", "by_direction"),
    ):
        lines.extend(["", f"## {title}", ""])
        rows = metrics[key]
        if rows:
            columns = list(next(iter(rows.values())))
            lines += ["| Item | " + " | ".join(columns) + " |", "|---|" + "---:|" * len(columns)]
            for item, values in rows.items():
                lines.append(
                    f"| {item} | "
                    + " | ".join(
                        f"{values[c]:.8g}" if values[c] is not None else "None" for c in columns
                    )
                    + " |"
                )
    lines += [
        "",
        "## Closed lifecycle returns in R",
        "",
        "Quantiles: " + str(metrics["r_distribution"]["quantiles"]),
    ]
    for label in ("best_five", "worst_five"):
        lines += [
            "",
            f"### {label}",
            "",
            "| Symbol | Direction | Entry ms | Exit ms | R | Net USD |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for p in metrics["r_distribution"][label]:
            lines.append(
                f"| {p['symbol']} | {p.get('direction', '')} | {p['entry_time_ms']} | "
                f"{p['exit_time_ms']} | {p['return_r']:.6g} | {p['net_pnl_usd']:.6g} |"
            )
    lines += ["", "## Assumptions", "", *(f"- {s}" for s in ASSUMPTIONS), ""]
    return "\n".join(lines)


def _git_state() -> dict[str, Any]:
    try:

        def run(*args: str) -> str:
            return subprocess.check_output(
                ["git", *args],
                text=True,
                cwd=Path(__file__).resolve().parents[3],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()

        return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _charts(result: BacktestResult, directory: Path) -> None:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    frame = pd.DataFrame(result.equity)
    times = pd.to_datetime(frame["time_ms"], unit="ms", utc=True)
    values = frame["equity_usd"]
    for name, series, label in (
        ("equity", values, "Equity, USD"),
        ("drawdown", 100 * (values / values.cummax() - 1), "Drawdown, %"),
    ):
        figure = Figure(figsize=(10, 4), layout="constrained")
        FigureCanvasAgg(figure)
        axis = figure.subplots()
        axis.plot(times, series, linewidth=1.1)
        axis.set_ylabel(label)
        axis.set_xlabel("UTC")
        axis.grid(alpha=0.25)
        figure.savefig(directory / f"{name}.png", dpi=140)
        figure.clear()


def save_report(
    reports_dir: Path,
    experiment: Experiment,
    toml: str,
    result: BacktestResult,
    metrics: dict[str, Any],
    provenance: list[dict[str, Any]],
    clock: Clock,
) -> Path:
    created_ms = clock.now_ms()
    stamp = datetime.fromtimestamp(created_ms / 1000, UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = reports_dir / f"{experiment.run.name}-{stamp}"
    reports_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"report already exists: {destination}")
    with TemporaryDirectory(prefix=".backtest-", dir=reports_dir) as temporary:
        directory = Path(temporary) / "result"
        directory.mkdir()
        for name in ("fills", "positions", "equity", "events"):
            pd.DataFrame(getattr(result, name)).to_parquet(
                directory / f"{name}.parquet", index=False
            )
        manifest = {
            "schema_version": 1,
            "created_at_ms": created_ms,
            "experiment_toml": toml,
            "resolved_config": experiment.model_dump(mode="json"),
            "git": _git_state(),
            "package_version": __version__,
            "inputs": provenance,
            "assumptions": ASSUMPTIONS,
        }
        for name, value in (("metrics", metrics), ("manifest", manifest)):
            (directory / f"{name}.json").write_text(
                json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False)
                + "\n",
                encoding="utf-8",
            )
        (directory / "report.md").write_text(
            report_markdown(experiment.run.name, metrics), encoding="utf-8"
        )
        _charts(result, directory)
        directory.rename(destination)
    return destination
