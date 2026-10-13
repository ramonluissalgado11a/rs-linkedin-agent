"""Structured JSON logging with secret redaction and run/correlation IDs."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

SENSITIVE_KEYS = re.compile(
    r"(token|secret|password|passwd|cookie|authorization|api[_-]?key|client[_-]?secret|refresh|credential)",
    re.IGNORECASE,
)
# Values that look like credentials even when they appear inside free text.
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)bearer\s+[a-z0-9._\-~+/]+=*"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"AQ[A-Za-z0-9_\-]{30,}"),  # LinkedIn access tokens commonly start with AQ
    re.compile(r"(?i)(access_token|refresh_token|client_secret|code)=([^&\s]+)"),
]
REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    out = text
    for pattern in SENSITIVE_PATTERNS:
        replacement: Any = (lambda m: f"{m.group(1)}={REDACTED}") if pattern.groups >= 2 else REDACTED
        out = pattern.sub(replacement, out)
    return out


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets from dicts, lists and strings."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and SENSITIVE_KEYS.search(k) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
            "run_id": run_id_var.get(),
            "correlation_id": correlation_id_var.get(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


def new_id() -> str:
    return uuid.uuid4().hex
