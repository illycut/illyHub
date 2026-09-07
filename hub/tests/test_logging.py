from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from illyhub_hub.logsetup import JsonFormatter, configure_logging, correlation_id, get_logger


def test_json_formatter_includes_fields_and_correlation() -> None:
    token = correlation_id.set("abc123")
    try:
        record = logging.LogRecord(
            "adapter.heos", logging.WARNING, __file__, 1, "hello %s", ("x",), None
        )
        record.extra = {"host": "1.2.3.4"}
        out = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id.reset(token)
    assert out["level"] == "WARNING" and out["component"] == "adapter.heos"
    assert (
        out["msg"] == "hello x" and out["correlation_id"] == "abc123" and out["host"] == "1.2.3.4"
    )


def test_json_formatter_renders_exceptions() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "fail", None, sys.exc_info())
    out = json.loads(JsonFormatter().format(record))
    assert "ValueError: bad" in out["exc"]


def test_configure_logging_stdout_only() -> None:
    configure_logging("debug")
    root = logging.getLogger()
    assert root.level == logging.DEBUG and len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.handlers[0].level == logging.DEBUG  # stdout follows level without a log file
    assert logging.getLogger("uvicorn.access").propagate is True
    assert get_logger("api").name == "api"


def test_configure_logging_with_rotating_file_quiets_stdout(tmp_path) -> None:
    log_file = tmp_path / "hub.log"
    configure_logging("INFO", str(log_file))
    root = logging.getLogger()
    stdout, rotating = root.handlers
    assert stdout.level == logging.WARNING  # default when a log file is set
    assert isinstance(rotating, RotatingFileHandler)
    assert rotating.maxBytes == 10 * 1024 * 1024 and rotating.backupCount == 7
    get_logger("test").info("to file")
    for h in root.handlers:
        h.flush()
    assert "to file" in log_file.read_text()
    configure_logging("INFO", str(log_file), stdout_level="ERROR")
    assert logging.getLogger().handlers[0].level == logging.ERROR
    for h in logging.getLogger().handlers:
        h.close()
    configure_logging("INFO")
