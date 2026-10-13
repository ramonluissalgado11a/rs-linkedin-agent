"""Action proposal + human-in-the-loop approval queue.

Pending items are never executed automatically. Approve on an API-backed item
enqueues an execution job; approve on a MANUAL_ACTION_REQUIRED item only marks
it as acknowledged — the human performs it in LinkedIn and taps "Done".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import jobs
from .config import Settings
from .db import Database, dumps, loads, utcnow
from .errors import AgentError, ErrorCategory, audit
from .logging_setup import new_id
from .policy import PolicyDecision, PolicyState, ProposedAction, evaluate

QUEUE_STATES = {PolicyState.MANUAL_APPROVAL_REQUIRED, PolicyState.MANUAL_ACTION_REQUIRED}


@dataclass
class ProposalResult:
    action_id: str | None
    queue_id: str | None
    decision: PolicyDecision


def propose(db: Database, settings: Settings, action: ProposedAction, actor: str = "agent") -> ProposalResult:
    decision = evaluate(db, settings, action)
    now = utcnow()
    if decision.state == PolicyState.BLOCKED_DUPLICATE:
        # Never store duplicates; record why in the audit log only.
        audit(db, actor, "action_blocked_duplicate", "action", None,
              {"type": action.action_type, "target": action.target_id, "reasons": decision.reasons})
        return ProposalResult(None, None, decision)

    status = {
        PolicyState.APPROVED_AUTOMATION: "approved",
        PolicyState.MANUAL_APPROVAL_REQUIRED: "pending_approval",
        PolicyState.MANUAL_ACTION_REQUIRED: "manual_pending",
    }.get(decision.state, "blocked")
    action_id = new_id()
    queue_id: str | None = None
    with db.transaction():
        db.execute(
            "INSERT INTO actions (id, action_type, target_kind, target_id, target_label, idempotency_key, policy_state,"
            " policy_reasons, permission_used, risk, payload, source_id, reason, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id, action.action_type, action.target_kind, action.target_id, action.target_label,
                decision.idempotency_key, decision.state.value, dumps(decision.reasons), decision.permission_used,
                decision.risk, dumps({"content": action.content}), action.source_id, action.reason, status, now, now,
            ),
        )
        if decision.state in QUEUE_STATES:
            queue_id = new_id()
            db.execute(
                "INSERT INTO action_queue (id, action_id, state, status, proposed_content, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (queue_id, action_id, decision.state.value, action.content, now, now),
            )
        if decision.state == PolicyState.APPROVED_AUTOMATION:
            jobs.enqueue(db, "execute_action", {"action_id": action_id}, f"exec:{action_id}",
                         max_attempts=settings.job_max_attempts)
        audit(db, actor, "action_proposed", "action", action_id,
              {"type": action.action_type, "state": decision.state.value, "reasons": decision.reasons})
    return ProposalResult(action_id, queue_id, decision)


def list_queue(db: Database, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT q.id AS queue_id, q.state, q.status, q.proposed_content, q.edited_content, q.created_at,"
        " a.id AS action_id, a.action_type, a.target_kind, a.target_id, a.target_label, a.reason, a.risk,"
        " a.permission_used, a.policy_reasons, a.source_id, s.name AS source_name, s.url AS source_url"
        " FROM action_queue q JOIN actions a ON a.id = q.action_id LEFT JOIN sources s ON s.id = a.source_id"
        " WHERE q.status = ? ORDER BY q.created_at ASC LIMIT ?",
        (status, limit),
    )
    for r in rows:
        r["policy_reasons"] = loads(r["policy_reasons"]) or []
    return rows


def _get(db: Database, queue_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT q.*, a.action_type, a.id AS act_id FROM action_queue q JOIN actions a ON a.id = q.action_id"
        " WHERE q.id = ?", (queue_id,))
    if row is None:
        raise AgentError("queue item not found", ErrorCategory.VALIDATION)
    return row


def decide(db: Database, settings: Settings, queue_id: str, decision: str, actor: str,
           edited_content: str | None = None, note: str | None = None) -> dict[str, Any]:
    if decision not in {"approve", "reject", "edit", "skip", "mark_done"}:
        raise AgentError("invalid decision", ErrorCategory.VALIDATION)
    item = _get(db, queue_id)
    if item["status"] != "pending" and not (decision == "mark_done" and item["status"] == "approved"):
        raise AgentError(f"queue item is already {item['status']}", ErrorCategory.VALIDATION)
    now = utcnow()
    with db.transaction():
        db.execute(
            "INSERT INTO approvals (id, queue_id, decision, actor, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), queue_id, decision, actor, note, now),
        )
        if decision == "edit":
            if not edited_content or len(edited_content) > 3000:
                raise AgentError("edited content must be 1–3000 characters", ErrorCategory.VALIDATION)
            db.execute("UPDATE action_queue SET edited_content = ?, updated_at = ? WHERE id = ?",
                       (edited_content, now, queue_id))
            db.execute("UPDATE actions SET payload = ?, updated_at = ? WHERE id = ?",
                       (dumps({"content": edited_content}), now, item["act_id"]))
        elif decision == "reject":
            db.execute("UPDATE action_queue SET status = 'rejected', updated_at = ? WHERE id = ?", (now, queue_id))
            db.execute("UPDATE actions SET status = 'rejected', updated_at = ? WHERE id = ?", (now, item["act_id"]))
        elif decision == "skip":
            db.execute("UPDATE action_queue SET status = 'skipped', updated_at = ? WHERE id = ?", (now, queue_id))
            db.execute("UPDATE actions SET status = 'skipped', updated_at = ? WHERE id = ?", (now, item["act_id"]))
        elif decision == "approve":
            db.execute("UPDATE action_queue SET status = 'approved', updated_at = ? WHERE id = ?", (now, queue_id))
            if item["state"] == PolicyState.MANUAL_APPROVAL_REQUIRED.value:
                db.execute("UPDATE actions SET status = 'approved', updated_at = ? WHERE id = ?", (now, item["act_id"]))
                jobs.enqueue(db, "execute_action", {"action_id": item["act_id"]}, f"exec:{item['act_id']}",
                             max_attempts=settings.job_max_attempts)
            else:
                # Manual action: approval means "I will do it". Nothing is executed by the system.
                db.execute("UPDATE actions SET status = 'manual_pending', updated_at = ? WHERE id = ?",
                           (now, item["act_id"]))
        elif decision == "mark_done":
            if item["state"] != PolicyState.MANUAL_ACTION_REQUIRED.value:
                raise AgentError("only manual actions can be marked done", ErrorCategory.VALIDATION)
            db.execute("UPDATE action_queue SET status = 'done', updated_at = ? WHERE id = ?", (now, queue_id))
            db.execute("UPDATE actions SET status = 'executed', executed_at = ?, updated_at = ? WHERE id = ?",
                       (now, now, item["act_id"]))
        audit(db, actor, f"queue_{decision}", "action_queue", queue_id, {"note": note})
    return _get(db, queue_id)
