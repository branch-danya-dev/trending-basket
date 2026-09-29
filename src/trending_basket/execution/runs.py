"""Explicit run lifecycle, immutable configuration and code/account identity."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from trending_basket.clock import Clock
from trending_basket.config import Settings
from trending_basket.execution.bybit_private import BybitPrivateClient
from trending_basket.execution.cycle import risk_config
from trending_basket.execution.journal import Halted, Journal
from trending_basket.execution.ledger import atomic_json, digest


def root(settings: Settings) -> Path:
    return settings.data_dir / "live" / "demo"


def configuration(settings: Settings) -> dict[str, Any]:
    config, threshold, _ = risk_config(settings.demo_risk_file)
    return dict(
        strategy=config.run.strategy,
        interval=config.run.interval.value,
        parameters=config.strategy_params,
        limits=config.limits.model_dump(mode="json"),
        costs=config.costs.model_dump(mode="json"),
        quantity_mode="exchange",
        leverage=3,
        allocated_capital_usd=settings.allocated_capital_usd,
        stop_drawdown_frac=threshold,
        mode=settings.mode,
        universe="demo-core15",
    )


def code_identity() -> dict[str, str]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    files = sorted(Path("src/trending_basket").rglob("*.py"))
    hashes = {p.as_posix(): digest(p.read_text(encoding="utf-8")) for p in files}
    critical = {
        p: h
        for p, h in hashes.items()
        if any(f"/{n}/" in p for n in ("strategies", "portfolio", "execution"))
    }
    return dict(commit=commit, source_hash=digest(hashes), critical_hash=digest(critical))


def active_directory(settings: Settings) -> Path:
    pointer = root(settings) / "active_run"
    if not pointer.exists():
        raise Halted("no active run; use tb run start --name demo-001")
    run_id = str(json.loads(pointer.read_text(encoding="utf-8"))["run_id"])
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", run_id):
        raise Halted("invalid active_run pointer")
    path = root(settings) / "runs" / run_id
    if not (path / "manifest.json").exists():
        raise Halted("active run manifest missing")
    return path


def validate(
    journal: Journal,
    settings: Settings,
    client: BybitPrivateClient,
    *,
    allow_code_change: bool = False,
) -> None:
    manifest = json.loads((journal.directory / "manifest.json").read_text(encoding="utf-8"))
    genesis = journal.read_rows("lifecycle")
    if not genesis or genesis[0].get("manifest") != manifest:
        raise Halted("run manifest does not match journal")
    if journal.state.get("run_status") != "active":
        raise Halted("run is closed")
    if digest(configuration(settings)) != manifest["config_hash"]:
        raise Halted("run configuration changed; use tb run close and tb run start")
    if client.account_fingerprint(manifest["run_id"]) != manifest["account_hash"]:
        raise Halted("run account differs from manifest")
    current = code_identity()
    previous = journal.state.get("code_identity", manifest["code"])
    if current != previous:
        substantial = current["critical_hash"] != previous["critical_hash"]
        if substantial and not allow_code_change:
            raise Halted("execution/strategy/portfolio code changed; require --allow-code-change")
        journal.append(
            "events",
            kind="code_version_change",
            previous=previous,
            current=current,
            substantial=substantial,
        )
        journal.state["code_identity"] = current
        journal.save()


def create(
    settings: Settings,
    clock: Clock,
    client: BybitPrivateClient,
    name: str,
    *,
    legacy: Journal | None = None,
) -> Journal:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise ValueError("run name must contain 1..64 letters, digits, underscores or hyphens")
    base = root(settings)
    if (base / "active_run").exists():
        raise Halted("an active run already exists")
    run_id = f"{name}-{clock.now_ms()}-{uuid4().hex[:8]}"
    resolved = configuration(settings)
    identity = code_identity()
    manifest = dict(
        run_id=run_id,
        name=name,
        created_ms=clock.now_ms(),
        git_commit=identity["commit"],
        code=identity,
        configuration=resolved,
        config_hash=digest(resolved),
        account_hash=client.account_fingerprint(run_id),
        status="active",
    )
    directory = base / "runs" / run_id
    directory.mkdir(parents=True)
    atomic_json(directory / "manifest.json", manifest)
    journal = Journal(directory, clock)
    if legacy is not None:
        journal.state = copy.deepcopy(legacy.state)
        journal.state.pop("journal_seq", None)
        journal.state.pop("journal_hash", None)
    journal.state.update(
        run_id=run_id,
        run_status="active",
        code_identity=identity,
        restart_count=0,
        downtime_ms=0,
        snapshot_time_ms=clock.now_ms(),
    )
    journal.append(
        "lifecycle",
        kind="run_created",
        manifest=manifest,
        initial_state=copy.deepcopy(journal.state),
        adopted_legacy=legacy is not None,
    )
    if legacy is not None:
        for channel in (
            "decisions",
            "orders",
            "fills",
            "positions",
            "equity",
            "events",
            "shadow",
            "funding",
        ):
            for row in legacy.read_rows(channel):
                journal.append("legacy", original_channel=channel, record=row)
    journal.save()
    atomic_json(base / "active_run", {"run_id": run_id})
    return journal


def close(journal: Journal, settings: Settings) -> None:
    if journal.state["positions"] or journal.state["pending_order"] or journal.state["pending"]:
        raise Halted("cannot close run with positions or pending intents; exchange stops preserved")
    journal.state["run_status"] = "closed"
    journal.append("lifecycle", kind="run_closed", final_state=copy.deepcopy(journal.state))
    journal.save()
    atomic_json(journal.directory / "final.json", journal.state)
    (root(settings) / "active_run").unlink()
