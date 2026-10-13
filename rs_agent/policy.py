"""Safety / compliance engine. Every external action passes these gates first.

check_permission → check_policy → check_rate_limit → check_duplicate →
check_recent_action → check_target → check_manual_approval_requirement

Only actions backed by an authorized LinkedIn API AND a granted scope can ever
reach APPROVED_AUTOMATION. Member-to-member interactions (follow, connect,
like, comment, message) have no authorized API for this app and are always
MANUAL_ACTION_REQUIRED: the agent drafts, the human acts in the LinkedIn app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from . import dedup
from .config import Settings
from .db import Database


class PolicyState(str, Enum):
    APPROVED_AUTOMATION = "APPROVED_AUTOMATION"
    MANUAL_APPROVAL_REQUIRED = "MANUAL_APPROVAL_REQUIRED"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    BLOCKED_POLICY = "BLOCKED_POLICY"
    BLOCKED_PERMISSION = "BLOCKED_PERMISSION"
    BLOCKED_DUPLICATE = "BLOCKED_DUPLICATE"
    BLOCKED_RATE_LIMIT = "BLOCKED_RATE_LIMIT"


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    api_backed: bool               # an authorized API exists for this action
    required_scope: str | None     # OAuth scope needed when api_backed
    risk: str
    human_response: bool = False   # uses third-party content → HUMAN_RESPONSE_REQUIRED


ACTION_CATALOG: dict[str, ActionSpec] = {
    # Own content via Posts API (w_member_social / w_organization_social).
    "CREATE_POST": ActionSpec("CREATE_POST", True, "w_member_social", "medium"),
    "CREATE_ORG_POST": ActionSpec("CREATE_ORG_POST", True, "w_organization_social", "medium"),
    # No authorized member API → always human-performed.
    "FOLLOW": ActionSpec("FOLLOW", False, None, "medium"),
    "CONNECT": ActionSpec("CONNECT", False, None, "high"),
    "REACT": ActionSpec("REACT", False, None, "medium"),
    "COMMENT": ActionSpec("COMMENT", False, None, "high", human_response=True),
    "MESSAGE": ActionSpec("MESSAGE", False, None, "high"),
}

# Things the system must never do, regardless of configuration.
FORBIDDEN_ACTIONS = {
    "SCRAPE", "BROWSER_AUTOMATION", "BULK_FOLLOW", "BULK_CONNECT", "BULK_MESSAGE", "BULK_COMMENT",
    "BULK_LIKE", "EVADE_DETECTION", "ROTATE_ACCOUNT",
}
RECENT_WINDOW = timedelta(days=30)


@dataclass
class ProposedAction:
    action_type: str
    target_kind: str | None = None
    target_id: str | None = None
    target_label: str | None = None
    content: str | None = None
    source_id: str | None = None
    reason: str | None = None


@dataclass
class PolicyDecision:
    state: PolicyState
    reasons: list[str] = field(default_factory=list)
    permission_used: str | None = None
    risk: str = "low"
    idempotency_key: str = ""
    human_response_required: bool = False

    @property
    def blocked(self) -> bool:
        return self.state.value.startswith("BLOCKED")


def granted_scopes(db: Database, provider: str = "linkedin") -> set[str]:
    rows = db.fetchall("SELECT scope FROM api_permissions WHERE provider = ? AND granted = 1", (provider,))
    return {r["scope"] for r in rows}


def _since(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat(timespec="seconds")


def evaluate(db: Database, settings: Settings, action: ProposedAction) -> PolicyDecision:
    fingerprint = dedup.content_hash(action.content) if action.content else ""
    key = dedup.action_key(action.action_type, action.target_id, fingerprint)
    decision = PolicyDecision(PolicyState.MANUAL_APPROVAL_REQUIRED, idempotency_key=key)

    # check_policy (first: forbidden or unknown actions never proceed)
    if action.action_type in FORBIDDEN_ACTIONS:
        decision.state = PolicyState.BLOCKED_POLICY
        decision.reasons.append("FORBIDDEN_ACTION")
        return decision
    spec = ACTION_CATALOG.get(action.action_type)
    if spec is None:
        decision.state = PolicyState.BLOCKED_POLICY
        decision.reasons.append("UNKNOWN_ACTION")
        return decision
    decision.risk = spec.risk
    decision.human_response_required = spec.human_response
    if action.content is not None and len(action.content) > 3000:
        decision.state = PolicyState.BLOCKED_POLICY
        decision.reasons.append("CONTENT_TOO_LONG")
        return decision

    # check_duplicate (same action + target + content already exists)
    if db.fetchone("SELECT id FROM actions WHERE idempotency_key = ?", (key,)):
        decision.state = PolicyState.BLOCKED_DUPLICATE
        decision.reasons.append("DUPLICATE_ACTION")
        return decision
    if action.content and db.fetchone(
        "SELECT id FROM content WHERE content_hash = ? AND status IN ('published', 'queued')",
        (dedup.content_hash(action.content),),
    ):
        decision.state = PolicyState.BLOCKED_DUPLICATE
        decision.reasons.append("DUPLICATE_CONTENT")
        return decision

    # check_target
    if spec.action_type not in ("CREATE_POST", "CREATE_ORG_POST"):
        if not action.target_id:
            decision.state = PolicyState.BLOCKED_POLICY
            decision.reasons.append("MISSING_TARGET")
            return decision
        if action.target_kind == "profile":
            prof = db.fetchone("SELECT status FROM profiles WHERE id = ?", (action.target_id,))
            if prof is None or prof["status"] != "qualified":
                decision.state = PolicyState.BLOCKED_POLICY
                decision.reasons.append("TARGET_NOT_QUALIFIED")
                return decision

    # check_recent_action (same type to same target in the last 30 days)
    if action.target_id and db.fetchone(
        "SELECT id FROM actions WHERE action_type = ? AND target_id = ? AND created_at >= ?"
        " AND status NOT IN ('rejected', 'skipped')",
        (spec.action_type, action.target_id, _since(RECENT_WINDOW)),
    ):
        decision.state = PolicyState.BLOCKED_DUPLICATE
        decision.reasons.append("RECENT_ACTION_SAME_TARGET")
        return decision

    if not spec.api_backed:
        # No authorized API: the human performs it. Content may be drafted only when it is
        # the user's own words or derived from information the user provided.
        decision.state = PolicyState.MANUAL_ACTION_REQUIRED
        decision.reasons.append("NO_AUTHORIZED_API")
        if spec.human_response:
            decision.reasons.append("HUMAN_RESPONSE_REQUIRED")
        return decision

    # check_permission
    if spec.required_scope not in granted_scopes(db):
        decision.state = PolicyState.BLOCKED_PERMISSION
        decision.reasons.append(f"MISSING_SCOPE:{spec.required_scope}")
        return decision
    decision.permission_used = spec.required_scope

    # check_rate_limit (counts everything that is or will be executed)
    per_hour, per_day = settings.rate_limit(spec.action_type)
    counted = "('approved','executed','pending_approval')"
    hour_n = int(db.scalar(
        f"SELECT COUNT(*) FROM actions WHERE action_type = ? AND created_at >= ? AND status IN {counted}",  # nosec B608 # noqa: S608 - constant status list, values parameterized
        (spec.action_type, _since(timedelta(hours=1))),
    ) or 0)
    day_n = int(db.scalar(
        f"SELECT COUNT(*) FROM actions WHERE action_type = ? AND created_at >= ? AND status IN {counted}",  # nosec B608 # noqa: S608 - constant status list, values parameterized
        (spec.action_type, _since(timedelta(days=1))),
    ) or 0)
    if per_hour == 0 or per_day == 0 or hour_n >= per_hour or day_n >= per_day:
        decision.state = PolicyState.BLOCKED_RATE_LIMIT
        decision.reasons.append(f"RATE_LIMIT:{hour_n}/{per_hour}h,{day_n}/{per_day}d")
        return decision

    # check_manual_approval_requirement
    if settings.linkedin_auto_publish and spec.risk != "high":
        decision.state = PolicyState.APPROVED_AUTOMATION
        decision.reasons.append("AUTO_PUBLISH_ENABLED")
    else:
        decision.state = PolicyState.MANUAL_APPROVAL_REQUIRED
        decision.reasons.append("APPROVAL_REQUIRED")
    return decision
