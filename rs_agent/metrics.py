"""Observability: dashboard metrics and component health."""

from __future__ import annotations

from typing import Any

from .claude import ClaudeClient
from .db import Database
from .jobs import queue_depth
from .linkedin import LinkedInClient
from .qualification import qualification_stats

HEALTHY, DEGRADED, BLOCKED, FAILED = "HEALTHY", "DEGRADED", "BLOCKED", "FAILED"


def action_counts(db: Database) -> dict[str, int]:
    rows = db.fetchall("SELECT status, COUNT(*) AS n FROM actions GROUP BY status")
    counts = {r["status"]: int(r["n"]) for r in rows}
    return {
        "actions_proposed": sum(counts.values()),
        "actions_approved": counts.get("approved", 0) + counts.get("executed", 0),
        "actions_rejected": counts.get("rejected", 0),
        "actions_executed": counts.get("executed", 0),
        "actions_failed": counts.get("failed", 0),
        "actions_blocked": counts.get("blocked", 0),
        "pending_approvals": int(db.scalar("SELECT COUNT(*) FROM action_queue WHERE status = 'pending'") or 0),
    }


def content_counts(db: Database) -> dict[str, int]:
    return {
        "posts_generated": int(db.scalar("SELECT COUNT(*) FROM content WHERE kind = 'post'") or 0),
        "posts_published": int(db.scalar("SELECT COUNT(*) FROM posts WHERE status = 'published'") or 0),
    }


def provider_errors(db: Database) -> dict[str, int]:
    rows = db.fetchall("SELECT category, COUNT(*) AS n FROM errors GROUP BY category")
    return {r["category"]: int(r["n"]) for r in rows}


def avg_latency(db: Database, name: str) -> float | None:
    value = db.scalar(
        "SELECT AVG(value) FROM (SELECT value FROM metrics WHERE name = ? ORDER BY created_at DESC LIMIT 50) t", (name,))
    return round(float(value), 1) if value is not None else None


def component_health(db: Database, linkedin: LinkedInClient, claude: ClaudeClient) -> dict[str, Any]:
    db_ok = db.ping()
    database = {"status": HEALTHY if db_ok else FAILED}
    if not db_ok:
        return {"system": FAILED, "database": database, "linkedin": {"status": "UNKNOWN"},
                "claude": {"status": "UNKNOWN"}, "queue": {"status": "UNKNOWN"}}
    depth = queue_depth(db)
    queue = {"status": DEGRADED if depth.get("dead", 0) else HEALTHY, "depth": depth}
    li = linkedin.status()
    cl = claude.status()
    statuses = [database["status"], queue["status"], li["status"], cl["status"]]
    # A LinkedIn or Claude outage degrades the system; it never takes it down.
    system = FAILED if database["status"] == FAILED else (DEGRADED if any(s != HEALTHY for s in statuses) else HEALTHY)
    return {"system": system, "database": database, "linkedin": li, "claude": cl, "queue": queue}


def dashboard_data(db: Database, linkedin: LinkedInClient, claude: ClaudeClient) -> dict[str, Any]:
    health = component_health(db, linkedin, claude)
    if health["system"] == FAILED:
        return {"health": health}
    q = qualification_stats(db)
    return {
        "health": health,
        "qualification": q,
        "actions": action_counts(db),
        "content": content_counts(db),
        "errors": provider_errors(db),
        "latency_ms": {"linkedin": avg_latency(db, "linkedin_api_latency_ms"),
                       "claude": avg_latency(db, "claude_latency_ms")},
        "cost": health["claude"] if "daily_cost_usd" in health["claude"] else {},
        "cost_per_candidate_usd": _per(db, "qualify"),
        "cost_per_content_usd": _per(db, "post_draft"),
    }


def _per(db: Database, purpose: str) -> float | None:
    row = db.fetchone("SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS c FROM llm_usage WHERE purpose = ?", (purpose,))
    if not row or not row["n"]:
        return None
    return round(float(row["c"]) / int(row["n"]), 4)
