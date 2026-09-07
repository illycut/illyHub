"""Structured JSON logging with a per-request correlation id.

Always logs to stdout. When ``log_file`` is set, a size-rotated file (10 MB x 7) receives the
full ``level`` stream and stdout drops to ``stdout_level`` (default WARNING) so launchd's
capture file stays small and acts only as a crash fallback.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

ROTATE_BYTES = 10 * 1024 * 1024
ROTATE_COUNT = 7


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line: ts, level, component, msg, correlation_id, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO", log_file: str | None = None, stdout_level: str | None = None
) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    formatter = JsonFormatter()

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    stdout.setLevel((stdout_level or ("WARNING" if log_file else level)).upper())
    root.addHandler(stdout)

    if log_file:
        rotating = RotatingFileHandler(log_file, maxBytes=ROTATE_BYTES, backupCount=ROTATE_COUNT)
        rotating.setFormatter(formatter)
        rotating.setLevel(level.upper())
        root.addHandler(rotating)

    root.setLevel(level.upper())
    # Route uvicorn through the same handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(component)
