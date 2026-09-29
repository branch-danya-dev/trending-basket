"""One authoritative, hash-chained write-ahead journal per managed run."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

ZERO_HASH = "0" * 64


def encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class LedgerError(ValueError):
    pass


class Ledger:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "journal.jsonl"
        self.records: list[dict[str, Any]] = []

    def verify(self, now_ms: int, *, repair: bool = False) -> str | None:
        self.records = []
        data = self.path.read_bytes() if self.path.exists() else b""
        offset = 0
        lines = data.splitlines(keepends=True)
        for index, raw in enumerate(lines):
            try:
                value = json.loads(raw)
                if not isinstance(value, dict) or not raw.endswith(b"\n"):
                    raise ValueError("incomplete line")
            except (ValueError, UnicodeError):
                if index != len(lines) - 1 or not repair:
                    raise LedgerError(f"invalid journal line {index + 1}") from None
                corrupt = self.path.with_name(f"journal.jsonl.corrupt-{now_ms}")
                with corrupt.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                with self.path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                return corrupt.name
            previous = self.records[-1]["hash"] if self.records else ZERO_HASH
            body = {k: v for k, v in value.items() if k != "hash"}
            if (
                value.get("seq") != index + 1
                or value.get("prev_hash") != previous
                or value.get("hash") != digest(body)
            ):
                raise LedgerError(f"journal chain mismatch at seq {index + 1}")
            self.records.append(value)
            offset += len(raw)
        return None

    def append(self, channel: str, now_ms: int, values: dict[str, Any]) -> dict[str, Any]:
        body = dict(
            values,
            channel=channel,
            type=values.get("kind", channel),
            time_ms=now_ms,
            seq=len(self.records) + 1,
            prev_hash=self.records[-1]["hash"] if self.records else ZERO_HASH,
        )
        row = dict(body, hash=digest(body))
        with self.path.open("ab") as stream:
            stream.write(encoded(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.records.append(copy.deepcopy(row))
        return row

    def restore(self, state: dict[str, Any]) -> dict[str, Any]:
        anchor = state.get("journal_seq", 0)
        if anchor:
            if anchor > len(self.records):
                raise LedgerError("snapshot is ahead of journal")
            row = self.records[anchor - 1]
            clean = {k: v for k, v in state.items() if k not in {"journal_seq", "journal_hash"}}
            if row.get("state") != clean or state.get("journal_hash") != row["hash"]:
                raise LedgerError("snapshot does not match its journal checkpoint")
        snapshots = [r for r in self.records if r["channel"] == "checkpoints"]
        if snapshots:
            last = snapshots[-1]
            return dict(last["state"], journal_seq=last["seq"], journal_hash=last["hash"])
        if anchor:
            raise LedgerError("missing journal checkpoint")
        return state
