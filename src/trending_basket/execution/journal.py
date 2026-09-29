"""Durable JSON state, append-only journals and an OS-owned process lock."""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from trending_basket.clock import Clock
from trending_basket.config import Settings


class Halted(RuntimeError):
    pass


class Journal:
    def __init__(self, directory: Path, clock: Clock) -> None:
        self.directory, self.clock = directory, clock
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.json"
        self.state: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else dict(
                schema=1,
                positions={},
                strategy_states={},
                orders={},
                stop_ids=[],
                processed_fills=[],
                pending=None,
                pending_order=None,
                last_decision_ms=None,
                last_poll_ms=None,
                peak_equity_usd=0.0,
                api_errors=0,
                halt=None,
            )
        )

    @contextmanager
    def locked(self) -> Iterator[None]:
        with (self.directory / "process.lock").open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Halted("another demo process owns the execution lock") from exc
            try:
                # State may have changed between construction and lock acquisition.
                if self.path.exists():
                    self.state = json.loads(self.path.read_text(encoding="utf-8"))
                yield
            finally:
                stream.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def append(self, channel: str, **values: Any) -> None:
        if channel not in {
            "decisions",
            "orders",
            "fills",
            "positions",
            "equity",
            "events",
            "shadow",
        }:
            raise ValueError("unknown journal")
        row = dict(time_ms=self.clock.now_ms(), **values)
        payload = (
            json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        ).encode()
        descriptor = os.open(
            self.directory / f"{channel}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError("incomplete journal write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def halt(self, reason: str) -> None:
        self.state["halt"] = dict(reason=reason, time_ms=self.clock.now_ms())
        self.save()
        self.append("events", kind="halt", reason=reason)

    def require_running(self) -> None:
        if self.state["halt"]:
            raise Halted(str(self.state["halt"]["reason"]))

    def operation_succeeded(self) -> None:
        if not self.state["halt"] and self.state["api_errors"]:
            self.state["api_errors"] = 0
            self.save()

    def api_error(self) -> None:
        self.state["api_errors"] += 1
        self.save()
        self.append("events", kind="api_error", consecutive=self.state["api_errors"])
        if self.state["api_errors"] >= 5:
            self.halt("five consecutive API errors; exchange stops preserved")


class Notifier:
    def __init__(
        self, settings: Settings, journal: Journal, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.silent = False
        self.token, self.chat = settings.telegram_bot_token, settings.telegram_chat_id
        self.journal, self.transport = journal, transport

    def send(self, message: str) -> bool:
        if self.silent:
            return True
        if not self.token or not self.chat:
            self.journal.append(
                "events", kind="telegram_unavailable", reason="configuration missing"
            )
            return False
        try:
            with httpx.Client(
                timeout=5, follow_redirects=False, transport=self.transport
            ) as client:
                result = client.post(
                    f"https://api.telegram.org/bot{self.token.get_secret_value()}/sendMessage",
                    json={"chat_id": self.chat.get_secret_value(), "text": message[:4000]},
                )
                result.raise_for_status()
                if not result.json().get("ok"):
                    raise ValueError("Telegram rejected notification")
        except (httpx.HTTPError, ValueError):
            # Exception repr contains the token-bearing URL; never log it.
            self.journal.append("events", kind="telegram_unavailable", reason="request failed")
            return False
        return True


class PreviewJournal(Journal):
    """Isolated checkpoint for dry runs and read-only checks, without durable writes."""

    def __init__(self, original: Journal) -> None:
        self.directory, self.path, self.clock = original.directory, original.path, original.clock
        self.state = copy.deepcopy(original.state)
        self.rows: list[dict[str, Any]] = []

    def save(self) -> None:
        pass

    def append(self, channel: str, **values: Any) -> None:
        self.rows.append(dict(channel=channel, **values))
