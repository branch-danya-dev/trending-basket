"""Mechanical selection and holdout verdict; no parameter search or inference."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from trending_basket.backtest.config import Experiment
from trending_basket.backtest.periods import period_dates
from trending_basket.clock import ManualClock

CANDIDATES = frozenset({"V1x", "V2x", "V3x", "V4x", "H1", "H2", "H3"})


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def select_candidate(metrics_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for ident, metrics in sorted(metrics_by_id.items()):
        count = metrics.get("closed_positions")
        drawdown: Any = metrics.get("max_drawdown_frac")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"{ident}: closed_positions cannot be evaluated")
        if not finite(drawdown) or drawdown > 0:
            raise ValueError(f"{ident}: max_drawdown_frac cannot be evaluated")
        reasons = []
        if count < 100:
            reasons.append("closed_positions < 100")
        if drawdown < -0.20:
            reasons.append("max_drawdown_frac < -0.20")
        if not finite(metrics.get("sharpe")):
            reasons.append("sharpe undefined")
        candidates.append({"id": ident, "metrics": metrics, "exclusion_reasons": reasons})
    eligible = [row for row in candidates if not row["exclusion_reasons"]]
    eligible.sort(key=lambda row: (-row["metrics"]["sharpe"], row["id"]))
    return {
        "rule": "ADR-016",
        "period": "val",
        "candidates": candidates,
        "eligible_ranked": [row["id"] for row in eligible],
        "selected_id": eligible[0]["id"] if eligible else None,
        "holdout_allowed": bool(eligible),
    }


def holdout_verdict(candidate: dict[str, Any], btc: dict[str, Any]) -> dict[str, Any]:
    # Undefined SR and BTC volatility are explicit failures in ADR-016. Other
    # missing inputs cannot be reinterpreted: stop instead of inventing a verdict.
    for values, keys in (
        (candidate, ("max_drawdown_frac", "total_return_frac", "annual_volatility_frac")),
        (btc, ("total_return_frac",)),
    ):
        for key in keys:
            if not finite(values.get(key)):
                raise ValueError(f"{key} cannot be evaluated")
    if candidate["max_drawdown_frac"] > 0 or candidate["annual_volatility_frac"] < 0:
        raise ValueError("invalid candidate drawdown or volatility")
    btc_vol: Any = btc.get("annual_volatility_frac")
    denominator_valid = finite(btc_vol) and btc_vol > 0
    ratio = candidate["annual_volatility_frac"] / btc_vol if denominator_valid else None
    threshold = btc["total_return_frac"] * ratio if ratio is not None else None
    checks = {
        "positive_sharpe": {
            "value": candidate.get("sharpe"),
            "operator": ">",
            "threshold": 0,
            "passed": finite(candidate.get("sharpe")) and candidate["sharpe"] > 0,
        },
        "drawdown": {
            "value": candidate["max_drawdown_frac"],
            "operator": ">=",
            "threshold": -0.25,
            "passed": candidate["max_drawdown_frac"] >= -0.25,
        },
        "volatility_matched_btc": {
            "value": candidate["total_return_frac"],
            "operator": ">",
            "threshold": threshold,
            "volatility_ratio": ratio,
            "btc_volatility_valid": denominator_valid,
            "passed": threshold is not None and candidate["total_return_frac"] > threshold,
        },
    }
    passed = all(row["passed"] for row in checks.values())
    return {
        "rule": "ADR-016",
        "checks": checks,
        "passed": passed,
        "next_step": "T006 Demo with low risk"
        if passed
        else "T007 funding carry; stop trend family",
    }


def read_report(directory: Path, period: str, strategy: str) -> dict[str, Any]:
    metrics_bytes = (directory / "metrics.json").read_bytes()
    manifest_bytes = (directory / "manifest.json").read_bytes()
    metrics = json.loads(metrics_bytes)
    manifest = json.loads(manifest_bytes)
    config = Experiment.model_validate(manifest["resolved_config"])
    run = config.run
    if run.period != period or run.strategy != strategy:
        raise ValueError(f"{directory}: wrong period or strategy")
    if config.execution.quantity_mode != "exact" or run.initial_capital_usd != 10000:
        raise ValueError(f"{directory}: T005 requires exact $10000")
    if metrics.get("quantity_mode") != "exact" or manifest.get("quantity_mode") != "exact":
        raise ValueError(f"{directory}: inconsistent quantity mode")
    assert run.period is not None
    if (run.start, run.end) != period_dates(run.period, ManualClock(manifest["created_at_ms"])):
        raise ValueError(f"{directory}: dates disagree with ADR-015")
    return {
        "id": run.name,
        "directory": directory.as_posix(),
        "metrics": metrics,
        "resolved_config": config.model_dump(mode="json"),
        "metrics_sha256": hashlib.sha256(metrics_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "git": manifest["git"],
    }


def without_period(config: dict[str, Any]) -> dict[str, Any]:
    return {
        **config,
        "run": {k: v for k, v in config["run"].items() if k not in {"period", "start", "end"}},
    }


def write_decision(path: Path, decision: dict[str, Any]) -> None:
    # Never silently replace a recorded research decision.
    payload = json.dumps(decision, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(payload + "\n")
