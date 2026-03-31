from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service_name": self._service_name,
            "message": record.getMessage(),
            "logger": record.name,
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        extra_keys = set(record.__dict__) - {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "taskName",
        }
        for key in extra_keys:
            payload[key] = record.__dict__[key]

        return json.dumps(payload, default=str)


def get_logger(service_name: str) -> logging.Logger:
    """Return a logger that emits structured JSON to stdout.

    Each log record contains at minimum:
        - timestamp  (ISO-8601 UTC)
        - level
        - service_name
        - message
    """
    logger = logging.getLogger(service_name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_JsonFormatter(service_name))

    logger.addHandler(handler)
    logger.propagate = False

    return logger
