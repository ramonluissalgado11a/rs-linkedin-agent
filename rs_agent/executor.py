"""Executes approved, API-backed actions. Re-checks policy invariants right before acting."""

from __future__ import annotations

from typing import Any

from .db import Database, loads, utcnow
from .errors import AgentError, ErrorCategory, audit
from .linkedin import LinkedInClient
from .logging_setup import new_id
from .policy import ACTION_CATALOG, PolicyState, granted_scopes


def execute_action(db: Database, linkedin: LinkedInClient, action_id: str) -> dict[str, Any]:
    action = db.fetchone("SELECT * FROM actions WHERE id = ?", (action_id,))
    if action is None:
        raise AgentError("action not found", ErrorCategory.VALIDATION)
    if action["status"] == "executed":
        return {"status": "executed", "idempotent": True, "external_ref": action["external_ref"]}
    if action["status"] != "approved":
        raise AgentError(f"action not approved (status={action['status']})", ErrorCategory.POLICY)
    if action["policy_state"] not in (PolicyState.APPROVED_AUTOMATION.value, PolicyState.MANUAL_APPROVAL_REQUIRED.value):
        raise AgentError("action is not executable by the system", ErrorCategory.POLICY)
    spec = ACTION_CATALOG.get(action["action_type"])
    if spec is None or not spec.api_backed:
        raise AgentError("no authorized API for this action", ErrorCategory.POLICY)
    if spec.required_scope not in granted_scopes(db):
        raise AgentError(f"missing scope {spec.required_scope}", ErrorCategory.PERMISSION)

    content = (loads(action["payload"]) or {}).get("content") or ""
    if action["action_type"] == "CREATE_POST":
        urn = linkedin.create_text_post(content)
    else:
        raise AgentError(f"{action['action_type']} execution not implemented", ErrorCategory.POLICY)

    now = utcnow()
    with db.transaction():
        db.execute("UPDATE actions SET status = 'executed', external_ref = ?, executed_at = ?, updated_at = ? WHERE id = ?",
                   (urn, now, now, action_id))
        target = action["target_id"] or ""
        content_id = target.split(":", 1)[1] if target.startswith("content:") else ""
        if content_id and db.fetchone("SELECT id FROM content WHERE id = ?", (content_id,)):
            db.execute("UPDATE content SET status = 'published' WHERE id = ?", (content_id,))
            db.execute(
                "INSERT INTO posts (id, content_id, action_id, linkedin_urn, author_urn, status, published_at, created_at)"
                " VALUES (?, ?, ?, ?, (SELECT member_urn FROM oauth_tokens WHERE provider = 'linkedin'), 'published', ?, ?)",
                (new_id(), content_id, action_id, urn, now, now),
            )
        audit(db, "system", "action_executed", "action", action_id, {"external_ref": urn})
    return {"status": "executed", "external_ref": urn}


def mark_failed(db: Database, action_id: str, message: str) -> None:
    db.execute("UPDATE actions SET status = 'failed', updated_at = ? WHERE id = ? AND status = 'approved'",
               (utcnow(), action_id))
    audit(db, "system", "action_failed", "action", action_id, {"error": message})
