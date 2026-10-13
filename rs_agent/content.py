"""Content engine: original, humanized posts about supply chain operations.

Drafts are checked against content history (exact hash + shingle similarity)
to reduce semantic repetition, then proposed as CREATE_POST actions that go
through the policy engine and, by default, the human approval queue.
"""

from __future__ import annotations

import re

from . import dedup
from .approvals import ProposalResult, propose
from .claude import ClaudeClient
from .config import Settings
from .db import Database, utcnow
from .errors import AgentError, ErrorCategory
from .logging_setup import new_id
from .policy import ProposedAction

TOPICS = (
    "Supply Chain", "Logistics", "Transportation", "Warehouse", "Fleet", "Inventory", "Procurement", "Operations",
)
BANNED_PHRASES = (
    "in today's fast-paced world", "in today's rapidly changing", "game changer", "game-changer",
    "unlock the power", "unlocking the power", "it is important to note", "in conclusion", "delve into",
    "comment yes", "like if you agree", "tag someone", "follow for more", "as we navigate",
)
SYSTEM_PROMPT = (
    "You write LinkedIn posts for Ramón Salgado, a logistics and transportation operations professional in "
    "Puerto Rico. Write in English, in his voice: practical, specific, plain technical language, varied sentence "
    "length, like an experienced operations lead explaining something to a colleague. Teach one operational "
    "lesson and connect it to KPIs, cost, service and safety. Never invent statistics, percentages, dollar values, "
    "employers, clients, credentials or personal experiences. No engagement bait, no clickbait, no generic AI "
    "phrases. 1,800–2,700 characters including a closing professional question and 6–10 relevant hashtags. "
    "Return only the post text."
)


def lint_post(body: str) -> list[str]:
    """Quality gates applied before a draft can be proposed."""
    problems = []
    lower = body.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            problems.append(f"banned phrase: {phrase}")
    if len(body) > 3000:
        problems.append("over 3000 characters")
    if len(body) < 300:
        problems.append("too short to teach anything")
    if re.search(r"\b\d{1,3}(\.\d+)?\s?%", body) and "illustrative" not in lower and "source:" not in lower:
        problems.append("contains a percentage without a source or 'illustrative' label")
    if re.search(r"\$\s?\d", body) and "illustrative" not in lower and "source:" not in lower:
        problems.append("contains a dollar figure without a source or 'illustrative' label")
    return problems


def history_shingles(db: Database, limit: int = 60) -> list[set[str]]:
    rows = db.fetchall("SELECT shingles FROM content WHERE kind = 'post' ORDER BY created_at DESC LIMIT ?", (limit,))
    return [set((r["shingles"] or "").split("|")) - {""} for r in rows]


def save_draft(db: Database, body: str, topic: str | None, model: str | None) -> str:
    chash = dedup.content_hash(body)
    if db.fetchone("SELECT id FROM content WHERE content_hash = ?", (chash,)):
        raise AgentError("duplicate content", ErrorCategory.VALIDATION)
    dup, score = dedup.is_semantic_duplicate(body, history_shingles(db))
    if dup:
        raise AgentError(f"too similar to previous content (similarity {score:.2f})", ErrorCategory.VALIDATION)
    content_id = new_id()
    db.execute(
        "INSERT INTO content (id, kind, topic, body, content_hash, shingles, model, status, created_at)"
        " VALUES (?, 'post', ?, ?, ?, ?, ?, 'draft', ?)",
        (content_id, topic, body, chash, "|".join(sorted(dedup.shingles(body))), model, utcnow()),
    )
    return content_id


def generate_post(db: Database, settings: Settings, claude: ClaudeClient, topic: str, angle: str) -> ProposalResult:
    """Generate → lint → dedupe → save draft → propose for approval. Atomic on failure."""
    if topic not in TOPICS:
        raise AgentError(f"unknown topic {topic}", ErrorCategory.VALIDATION)
    recent = db.fetchall("SELECT topic, substr(body, 1, 120) AS hook FROM content WHERE kind = 'post'"
                         " ORDER BY created_at DESC LIMIT 10")
    avoid = "\n".join(f"- {r['topic']}: {r['hook']}" for r in recent) or "- none yet"
    user = f"Topic: {topic}\nAngle: {angle}\nDo not repeat these recent hooks/angles:\n{avoid}"
    result = claude.complete(SYSTEM_PROMPT, user, complexity="complex", max_tokens=1400, purpose="post_draft")
    problems = lint_post(result.text)
    if problems:
        raise AgentError("draft failed quality gates: " + "; ".join(problems), ErrorCategory.VALIDATION)
    with db.transaction():
        content_id = save_draft(db, result.text, topic, result.model)
        proposal = propose(db, settings, ProposedAction(
            action_type="CREATE_POST", target_kind="self", target_id=f"content:{content_id}",
            target_label=f"Post: {topic}", content=result.text, reason=f"Content plan: {angle}"))
        db.execute("UPDATE content SET status = 'queued' WHERE id = ?", (content_id,))
    return proposal
