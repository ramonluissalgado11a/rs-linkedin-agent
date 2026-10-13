"""Wires settings, database and clients together for web and worker processes."""

from __future__ import annotations

from dataclasses import dataclass

from .claude import ClaudeClient
from .config import Settings, load_settings, validate_settings
from .db import Database, dumps, utcnow
from .linkedin import LinkedInClient
from .logging_setup import new_id

ALLOWED_SOURCE_KINDS = {"manual_entry", "csv_import", "public_web", "referral", "networking_assistant", "linkedin_api"}


@dataclass
class AppContext:
    settings: Settings
    db: Database
    linkedin: LinkedInClient
    claude: ClaudeClient
    startup_problems: list[str]


def build_context(settings: Settings | None = None, db: Database | None = None, strict: bool | None = None) -> AppContext:
    settings = settings or load_settings()
    problems = validate_settings(settings)
    strict = settings.is_production if strict is None else strict
    if strict and problems:
        raise SystemExit("Startup validation failed: " + "; ".join(problems))
    db = db or Database(settings.database_url)
    db.migrate()
    return AppContext(settings, db, LinkedInClient(settings, db), ClaudeClient(settings, db), problems)


def ensure_source(db: Database, kind: str, name: str, url: str | None = None, notes: str | None = None) -> str:
    if kind not in ALLOWED_SOURCE_KINDS:
        raise ValueError(f"source kind '{kind}' is not an authorized source")
    row = db.fetchone("SELECT id FROM sources WHERE kind = ? AND name = ? AND COALESCE(url, '') = ?",
                      (kind, name, url or ""))
    if row:
        return str(row["id"])
    source_id = new_id()
    db.execute("INSERT INTO sources (id, kind, name, url, authorized, notes, created_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
               (source_id, kind, name, url, notes, utcnow()))
    return source_id


def start_run(db: Database, kind: str) -> str:
    run_id = new_id()
    db.execute("INSERT INTO system_runs (id, kind, status, started_at) VALUES (?, ?, 'running', ?)", (run_id, kind, utcnow()))
    return run_id


def finish_run(db: Database, run_id: str, status: str, detail: dict[str, object] | None = None) -> None:
    db.execute("UPDATE system_runs SET status = ?, detail = ?, finished_at = ? WHERE id = ?",
               (status, dumps(detail or {}), utcnow(), run_id))
