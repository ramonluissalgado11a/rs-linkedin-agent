"""Thin database layer: SQLite for development/tests, PostgreSQL in production.

SQL is written with `?` placeholders and portable types (TEXT ids, ISO-8601 UTC
timestamps, JSON stored as TEXT) so the same migrations run on both engines.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class DatabaseError(RuntimeError):
    """Raised for database failures; callers classify it as an internal error."""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def loads(value: str | None) -> Any:
    return json.loads(value) if value else None


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.RLock()
        if url.startswith("sqlite:///"):
            self.dialect = "sqlite"
            path = url.removeprefix("sqlite:///")
            self._conn: Any = sqlite3.connect(path or ":memory:", check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            if path not in ("", ":memory:"):
                self._conn.execute("PRAGMA journal_mode=WAL")
        elif url.startswith(("postgres://", "postgresql://")):
            self.dialect = "postgres"
            try:
                import psycopg  # type: ignore[import-not-found]
                from psycopg.rows import dict_row  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - depends on deployment image
                raise DatabaseError("psycopg is required for PostgreSQL (pip install 'psycopg[binary]')") from exc
            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            raise DatabaseError("Unsupported DATABASE_URL scheme")

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            try:
                cur = self._conn.execute(self._sql(sql), tuple(params))
                return int(cur.rowcount or 0)
            except Exception as exc:
                raise DatabaseError(str(exc)) from exc

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                cur = self._conn.execute(self._sql(sql), tuple(params))
                return [dict(r) for r in cur.fetchall()]
            except Exception as exc:
                raise DatabaseError(str(exc)) from exc

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """All-or-nothing block: a failure (e.g. AI provider error mid-way) never leaves partial data."""
        with self._lock:
            depth = getattr(self, "_tx_depth", 0)
            savepoint = f"sp_{depth}"
            self._conn.execute("BEGIN" if depth == 0 else f"SAVEPOINT {savepoint}")
            self._tx_depth = depth + 1
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK" if depth == 0 else f"ROLLBACK TO SAVEPOINT {savepoint}")
                if depth:
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._conn.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._tx_depth = depth

    def ping(self) -> bool:
        try:
            return bool(self.scalar("SELECT 1") == 1)
        except DatabaseError:
            return False

    def migrate(self) -> list[str]:
        """Apply pending migrations in filename order; returns names applied."""
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        done = {r["name"] for r in self.fetchall("SELECT name FROM schema_migrations")}
        applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            statements = [s.strip() for s in path.read_text().split(";") if s.strip()]
            with self.transaction():
                for stmt in statements:
                    self._conn.execute(stmt)
                self._conn.execute(
                    self._sql("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)"),
                    (path.name, utcnow()),
                )
            applied.append(path.name)
        return applied

    def close(self) -> None:
        with self._lock:
            self._conn.close()
