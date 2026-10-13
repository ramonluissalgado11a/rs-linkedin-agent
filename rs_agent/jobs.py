"""Database-backed job queue with retries, exponential backoff, dead-lettering,
idempotency, timeouts and a per-provider circuit breaker.

Retries are bounded (max_attempts); only retryable error categories are retried.
"""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, DatabaseError, dumps, loads, utcnow
from .errors import AgentError, ErrorCategory, record_error
from .logging_setup import new_id

Handler = Callable[[dict[str, Any]], Any]


def _at(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def enqueue(db: Database, kind: str, payload: dict[str, Any], idempotency_key: str,
            max_attempts: int = 5, delay_s: float = 0) -> str | None:
    """Insert a job unless one with the same idempotency key exists. Returns job id or None if duplicate."""
    if db.fetchone("SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)):
        return None
    job_id = new_id()
    now = utcnow()
    try:
        db.execute(
            "INSERT INTO jobs (id, kind, idempotency_key, payload, status, attempts, max_attempts, run_after,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
            (job_id, kind, idempotency_key, dumps(payload), max_attempts, _at(delay_s), now, now),
        )
    except DatabaseError:
        # Unique-constraint race: another worker inserted it first.
        return None
    return job_id


def backoff_seconds(attempt: int, base: float = 5.0, cap: float = 900.0, jitter: bool = True) -> float:
    delay = float(min(cap, base * (2 ** max(attempt - 1, 0))))
    return float(delay * (0.5 + random.random() / 2)) if jitter else delay  # nosec B311 # noqa: S311 - jitter, not security related


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive failures; half-opens after `cooldown_s`."""

    threshold: int = 5
    cooldown_s: float = 300.0
    failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            # half-open after the cooldown: let one attempt through
            return time.monotonic() - self.opened_at >= self.cooldown_s

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self.opened_at >= self.cooldown_s else "open"


class JobRunner:
    def __init__(self, db: Database, handlers: dict[str, Handler], breakers: dict[str, CircuitBreaker] | None = None,
                 provider_of: dict[str, str] | None = None, timeout_s: float = 60.0) -> None:
        self.db = db
        self.handlers = handlers
        self.breakers = breakers or {}
        self.provider_of = provider_of or {}
        self.timeout_s = timeout_s
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def claim(self) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY run_after ASC LIMIT 1",
            (utcnow(),),
        )
        if row is None:
            return None
        # Optimistic claim: only one worker flips queued→running.
        claimed = self.db.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'queued'",
            (utcnow(), row["id"]),
        )
        if claimed != 1:
            return None
        row["attempts"] += 1
        return row

    def run_once(self) -> str | None:
        """Process at most one job. Returns the resulting job status, or None when idle."""
        job = self.claim()
        if job is None:
            return None
        provider = self.provider_of.get(job["kind"])
        breaker = self.breakers.get(provider) if provider else None
        if breaker is not None and not breaker.allow():
            self._reschedule(job, ErrorCategory.PROVIDER, f"circuit open for {provider}", breaker.cooldown_s,
                             count_attempt=False)
            return "queued"
        handler = self.handlers.get(job["kind"])
        if handler is None:
            return self._dead(job, ErrorCategory.INTERNAL, f"no handler for {job['kind']}")
        try:
            future = self._pool.submit(handler, loads(job["payload"]) or {})
            future.result(timeout=self.timeout_s)
        except concurrent.futures.TimeoutError:
            if breaker:
                breaker.failure()
            return self._retry_or_dead(job, ErrorCategory.TEMPORARY, "job timed out", None)
        except AgentError as exc:
            if breaker and exc.category in (ErrorCategory.PROVIDER, ErrorCategory.TEMPORARY):
                breaker.failure()
            if exc.retryable:
                return self._retry_or_dead(job, exc.category, str(exc), exc.retry_after_s)
            return self._dead(job, exc.category, str(exc))
        except Exception as exc:  # noqa: BLE001 - unknown errors are internal, not retried
            return self._dead(job, ErrorCategory.INTERNAL, f"{type(exc).__name__}: {exc}")
        if breaker:
            breaker.success()
        self.db.execute("UPDATE jobs SET status = 'succeeded', last_error = NULL, updated_at = ? WHERE id = ?",
                        (utcnow(), job["id"]))
        return "succeeded"

    def _retry_or_dead(self, job: dict[str, Any], category: ErrorCategory, message: str,
                       retry_after_s: int | None) -> str:
        if job["attempts"] >= job["max_attempts"]:
            return self._dead(job, category, f"max attempts reached: {message}")
        delay = retry_after_s if retry_after_s else backoff_seconds(job["attempts"])
        self._reschedule(job, category, message, delay)
        return "queued"

    def _reschedule(self, job: dict[str, Any], category: ErrorCategory, message: str, delay_s: float,
                    count_attempt: bool = True) -> None:
        sql = (
            "UPDATE jobs SET status = 'queued', run_after = ?, last_error = ?, error_category = ?, updated_at = ? WHERE id = ?"
            if count_attempt else
            "UPDATE jobs SET status = 'queued', run_after = ?, last_error = ?, error_category = ?, updated_at = ?,"
            " attempts = attempts - 1 WHERE id = ?"
        )
        self.db.execute(sql, (_at(delay_s), message[:1000], category.value, utcnow(), job["id"]))
        record_error(self.db, category, f"job:{job['kind']}", message)

    def _dead(self, job: dict[str, Any], category: ErrorCategory, message: str) -> str:
        self.db.execute(
            "UPDATE jobs SET status = 'dead', last_error = ?, error_category = ?, updated_at = ? WHERE id = ?",
            (message[:1000], category.value, utcnow(), job["id"]),
        )
        record_error(self.db, category, f"job:{job['kind']}", message)
        return "dead"

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def queue_depth(db: Database) -> dict[str, int]:
    rows = db.fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    return {r["status"]: int(r["n"]) for r in rows}


def requeue_dead(db: Database, job_id: str) -> bool:
    """Manual recovery for a dead-lettered job (admin action)."""
    n = db.execute(
        "UPDATE jobs SET status = 'queued', attempts = 0, run_after = ?, updated_at = ? WHERE id = ? AND status = 'dead'",
        (utcnow(), utcnow(), job_id),
    )
    return n == 1
