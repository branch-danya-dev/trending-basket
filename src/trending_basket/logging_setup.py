"""Stdlib logging configuration: UTC ISO-8601 timestamps, level from Settings."""

from __future__ import annotations

import logging
import time

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _UtcFormatter(logging.Formatter):
    """Formatter that renders record timestamps as UTC ISO-8601."""

    converter = staticmethod(time.gmtime)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created)) + "Z"


def setup_logging(log_level: str) -> None:
    """Configure the root logger to log at `log_level` with UTC ISO-8601 timestamps."""
    handler = logging.StreamHandler()
    handler.setFormatter(_UtcFormatter(_LOG_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)
