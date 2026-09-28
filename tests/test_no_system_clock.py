"""AST check: domain/strategies/portfolio/backtest never touch the system clock.

Trading-critical code must receive time only through an injected Clock or an
explicit timestamp (see docs/ARCHITECTURE.md). This scans those packages for
direct calls to system-clock functions, however they were imported.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_CHECKED_PACKAGES = ("domain", "strategies", "portfolio", "backtest", "universe")

_FORBIDDEN_CALLS = frozenset(
    {
        "time.time",
        "time.monotonic",
        "time.perf_counter",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.date.today",
    }
)

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "trending_basket"


def _collect_import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map each local import name to its fully-qualified origin."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolve_dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """Resolve a Name/Attribute call target to a fully-qualified dotted path."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve_dotted(node.value, aliases)
        return None if base is None else f"{base}.{node.attr}"
    return None


def find_forbidden_time_calls(source: str) -> list[str]:
    """Return a description of every forbidden system-clock call in `source`."""
    tree = ast.parse(source)
    aliases = _collect_import_aliases(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _resolve_dotted(node.func, aliases)
            if dotted in _FORBIDDEN_CALLS:
                violations.append(f"line {node.lineno}: {dotted}()")

    return violations


def _checked_files() -> Iterable[Path]:
    for package in _CHECKED_PACKAGES:
        yield from (_SRC_ROOT / package).rglob("*.py")


def test_no_system_clock_calls_in_trading_critical_packages() -> None:
    offenders: dict[Path, list[str]] = {}
    for path in _checked_files():
        violations = find_forbidden_time_calls(path.read_text(encoding="utf-8"))
        if violations:
            offenders[path] = violations
    assert not offenders, f"forbidden system-clock calls found: {offenders}"


def test_checker_detects_synthetic_forbidden_call() -> None:
    snippet = "from time import time\n\ndef now_ms() -> int:\n    return int(time() * 1000)\n"
    violations = find_forbidden_time_calls(snippet)
    assert violations
    assert "time.time" in violations[0]


def test_checker_detects_datetime_now() -> None:
    snippet = "import datetime\n\ndef bad():\n    return datetime.datetime.now()\n"
    violations = find_forbidden_time_calls(snippet)
    assert violations


def test_checker_allows_clock_protocol_usage() -> None:
    snippet = (
        "from trending_basket.clock import Clock\n\n"
        "def now_ms(clock: Clock) -> int:\n"
        "    return clock.now_ms()\n"
    )
    assert find_forbidden_time_calls(snippet) == []
