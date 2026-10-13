"""People-first qualification pipeline with reason codes.

candidate_discovered → identity_verified → employment_verified → industry_checked
→ role_checked → deduplicated → qualified/rejected

Filters are never relaxed to hit the qualification KPI; quality beats volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import dedup
from .db import Database, utcnow
from .logging_setup import new_id

INDUSTRY_KEYWORDS = {
    "supply chain", "logistics", "transportation", "transport", "fleet", "warehouse", "warehousing",
    "inventory", "procurement", "purchasing", "sourcing", "operations", "distribution", "shipping",
    "ocean freight", "freight", "port", "ports", "maritime", "manufacturing", "retail logistics",
    "3pl", "4pl", "fulfillment", "trucking", "drayage", "customs", "import", "export", "cold chain",
    "materials management", "demand planning", "s&op",
}
ROLE_KEYWORDS = {
    "manager", "director", "vp", "vice president", "head", "chief", "coo", "cpo", "supervisor", "lead",
    "planner", "analyst", "coordinator", "specialist", "engineer", "buyer", "dispatcher", "superintendent",
    "founder", "owner", "president", "recruiter", "talent", "professor", "instructor", "consultant",
}
SENIOR_ROLE_KEYWORDS = {"manager", "director", "vp", "vice president", "head", "chief", "coo", "cpo", "president", "founder", "owner"}

REGION_PRIORITY = {
    "puerto rico": 30, "united states": 25, "usa": 25, "us": 25, "canada": 18, "mexico": 18, "méxico": 18,
    "caribbean": 22, "caribe": 22, "central america": 15, "centroamérica": 15, "south america": 14,
    "sudamérica": 14, "spain": 14, "españa": 14, "europe": 12, "europa": 12, "asia-pacific": 10,
    "middle east": 10, "africa": 10,
}

EXCLUDED_ORGS = {"pepsi", "pepsico", "cbc"}
EXCLUDED_PEOPLE = {"jonathan duran"}
MAX_EVIDENCE_AGE_DAYS = 400


@dataclass
class Candidate:
    full_name: str
    source_id: str
    source_url: str | None = None
    role_title: str | None = None
    company_name: str | None = None
    industry: str | None = None
    region: str | None = None
    headline: str | None = None
    profile_url: str | None = None
    evidence_date: str | None = None  # ISO date the source confirmed name+role


@dataclass
class StageResult:
    stage: str
    passed: bool
    reason_code: str | None = None
    detail: str | None = None


@dataclass
class Qualification:
    profile_id: str | None
    qualified: bool
    score: int
    results: list[StageResult] = field(default_factory=list)

    @property
    def rejection_reason(self) -> str | None:
        for r in self.results:
            if not r.passed:
                return r.reason_code
        return None


def _contains_any(text: str, keywords: set[str]) -> bool:
    norm = " " + dedup.normalize_text(text) + " "
    return any(f" {dedup.normalize_text(k)} " in norm for k in keywords)


def _parse_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def evaluate(c: Candidate, today: date | None = None) -> tuple[list[StageResult], int]:
    """Pure evaluation (no DB). Returns stage results and a relevance score."""
    today = today or date.today()
    results: list[StageResult] = []
    name = (c.full_name or "").strip()
    norm_name = dedup.normalize_text(name)

    # identity_verified: real-looking full name + provenance from an authorized source
    if not c.source_id:
        results.append(StageResult("identity_verified", False, "NO_PROVENANCE", "candidate has no source"))
        return results, 0
    if len(norm_name.split()) < 2 or len(norm_name) > 80:
        results.append(StageResult("identity_verified", False, "INVALID_NAME", "full name required"))
        return results, 0
    if not (c.source_url or c.profile_url):
        results.append(StageResult("identity_verified", False, "NO_SOURCE_URL", "no URL confirming identity"))
        return results, 0
    if norm_name in EXCLUDED_PEOPLE:
        results.append(StageResult("identity_verified", False, "EXCLUDED_PERSON", None))
        return results, 0
    results.append(StageResult("identity_verified", True))

    # employment_verified: role + company present, evidence not stale, not an excluded org
    if not c.role_title or not c.company_name:
        results.append(StageResult("employment_verified", False, "NO_EMPLOYMENT_EVIDENCE", "role and company required"))
        return results, 0
    company_norm = dedup.normalize_text(c.company_name)
    if any(f" {org} " in f" {company_norm} " for org in EXCLUDED_ORGS):
        results.append(StageResult("employment_verified", False, "EXCLUDED_COMPANY", None))
        return results, 0
    if c.evidence_date:
        d = _parse_date(c.evidence_date)
        if d is None:
            results.append(StageResult("employment_verified", False, "INVALID_EVIDENCE_DATE", c.evidence_date))
            return results, 0
        if (today - d).days > MAX_EVIDENCE_AGE_DAYS:
            results.append(StageResult("employment_verified", False, "STALE_EVIDENCE", f"{(today - d).days} days old"))
            return results, 0
    else:
        results.append(StageResult("employment_verified", False, "NO_EVIDENCE_DATE", "source date required"))
        return results, 0
    results.append(StageResult("employment_verified", True))

    # industry_checked
    industry_text = " ".join(filter(None, [c.industry, c.headline, c.role_title, c.company_name]))
    if not _contains_any(industry_text, INDUSTRY_KEYWORDS):
        results.append(StageResult("industry_checked", False, "INDUSTRY_MISMATCH", None))
        return results, 0
    results.append(StageResult("industry_checked", True))

    # role_checked
    if not _contains_any(c.role_title, ROLE_KEYWORDS):
        results.append(StageResult("role_checked", False, "ROLE_MISMATCH", c.role_title))
        return results, 0
    results.append(StageResult("role_checked", True))

    score = 40
    if _contains_any(c.role_title, SENIOR_ROLE_KEYWORDS):
        score += 20
    region = dedup.normalize_text(c.region or "")
    score += max((v for k, v in REGION_PRIORITY.items() if f" {dedup.normalize_text(k)} " in f" {region} "), default=0)
    if c.headline and _contains_any(c.headline, INDUSTRY_KEYWORDS):
        score += 10
    return results, min(score, 100)


def qualify(db: Database, c: Candidate, today: date | None = None) -> Qualification:
    """Run the pipeline, deduplicate, persist profile + stage log. Idempotent per candidate."""
    results, score = evaluate(c, today)
    key = dedup.profile_key(c.full_name, c.company_name, c.profile_url)
    ukey = dedup.url_key(c.profile_url)

    existing = db.fetchone("SELECT id FROM profiles WHERE dedup_key = ?", (key,))
    if existing is None and ukey:
        existing = db.fetchone("SELECT id FROM profiles WHERE url_key = ?", (ukey,))
    if existing:
        # Duplicate person/URL: never create a second row; log the attempt against the existing profile.
        results.append(StageResult("deduplicated", False, "DUPLICATE_PROFILE", existing["id"]))
        with db.transaction():
            _log_stages(db, existing["id"], results)
        return Qualification(existing["id"], False, 0, results)

    passed_all = all(r.passed for r in results) and len(results) == 4
    if passed_all:
        results.append(StageResult("deduplicated", True))
    qualified = passed_all

    now = utcnow()
    profile_id = new_id()
    with db.transaction():
        db.execute(
            "INSERT INTO profiles (id, dedup_key, full_name, headline, role_title, company_name, industry, region,"
            " profile_url, url_key, source_id, source_url, evidence_date, stage, status, score, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile_id, key, c.full_name.strip(), c.headline, c.role_title, c.company_name, c.industry,
                c.region, c.profile_url, ukey, c.source_id, c.source_url, c.evidence_date,
                "qualified" if qualified else results[-1].stage,
                "qualified" if qualified else "rejected", score if qualified else 0, now, now,
            ),
        )
        _log_stages(db, profile_id, results)
    return Qualification(profile_id, qualified, score if qualified else 0, results)


def _log_stages(db: Database, profile_id: str, results: list[StageResult]) -> None:
    db.execute(
        "INSERT INTO qualifications (id, profile_id, stage, passed, reason_code, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id(), profile_id, "candidate_discovered", 1, None, None, utcnow()),
    )
    for r in results:
        db.execute(
            "INSERT INTO qualifications (id, profile_id, stage, passed, reason_code, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id(), profile_id, r.stage, 1 if r.passed else 0, r.reason_code, r.detail, utcnow()),
        )


def qualification_stats(db: Database) -> dict[str, Any]:
    checked = int(db.scalar("SELECT COUNT(*) FROM profiles") or 0)
    qualified = int(db.scalar("SELECT COUNT(*) FROM profiles WHERE status = 'qualified'") or 0)
    duplicates = int(db.scalar(
        "SELECT COUNT(*) FROM qualifications WHERE reason_code = 'DUPLICATE_PROFILE'") or 0)
    reasons = db.fetchall(
        "SELECT reason_code, COUNT(*) AS n FROM qualifications WHERE passed = 0 GROUP BY reason_code ORDER BY n DESC"
    )
    discovered = checked + duplicates
    return {
        "candidates_discovered": discovered,
        "profiles_checked": checked,
        "profiles_qualified": qualified,
        "qualification_rate": round(qualified / checked, 4) if checked else 0.0,
        "duplicate_rate": round(duplicates / discovered, 4) if discovered else 0.0,
        "rejection_reasons": {r["reason_code"]: r["n"] for r in reasons},
        "kpi_target": 0.70,
    }
