"""Error taxonomy, persistence and audit logging."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from .db import Database, dumps, utcnow
from .logging_setup import get_logger, log, new_id, redact_text, run_id_var


class ErrorCategory(str, Enum):
    TEMPORARY = "temporary"
    PROVIDER = "provider"
    PERMISSION = "permission"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    VALIDATION = "validation"
    POLICY = "policy"
    INTERNAL = "internal"


RETRYABLE = {ErrorCategory.TEMPORARY, ErrorCategory.PROVIDER, ErrorCategory.RATE_LIMIT}


class AgentError(Exception):
    """Base error carrying a category that drives retry decisions."""

    category: ErrorCategory = ErrorCategory.INTERNAL

    def __init__(self, message: str, category: ErrorCategory | None = None, retry_after_s: int | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE


def classify_http_status(status: int) -> ErrorCategory:
    if status == 401:
        return ErrorCategory.AUTHENTICATION
    if status == 403:
        return ErrorCategory.PERMISSION
    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if status in (400, 404, 409, 410, 412, 415, 422, 426):
        return ErrorCategory.VALIDATION
    if status >= 500:
        return ErrorCategory.PROVIDER
    return ErrorCategory.INTERNAL


_logger = get_logger("rs_agent.errors")


def record_error(db: Database, category: ErrorCategory, component: str, message: str) -> None:
    """Persist an error; never raises (error recording must not cascade failures)."""
    safe = redact_text(message)[:2000]
    log(_logger, logging.WARNING, "error recorded", category=category.value, component=component, error=safe)
    try:
        db.execute(
            "INSERT INTO errors (id, category, component, message, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), category.value, component, safe, run_id_var.get(), utcnow()),
        )
    except Exception:  # noqa: BLE001 - last-resort guard
        _logger.exception("failed to persist error")


def audit(db: Database, actor: str, event: str, subject_kind: str | None = None,
          subject_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
    from .logging_setup import redact

    db.execute(
        "INSERT INTO audit_log (id, actor, event, subject_kind, subject_id, detail, run_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), actor, event, subject_kind, subject_id, dumps(redact(detail or {})), run_id_var.get(), utcnow()),
    )
