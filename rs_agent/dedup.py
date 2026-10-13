"""Deduplication: stable keys for people, companies, URLs, actions and content."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|s\.a|sa|de c\.v|gmbh|plc|lp|llp)\b\.?",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(_WORD.findall(text.lower()))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def company_key(name: str, website: str | None = None) -> str:
    if website:
        host = url_key(website)
        if host:
            return "co:" + sha(host.split("/")[0])
    base = normalize_text(_COMPANY_SUFFIXES.sub(" ", name))
    return "co:" + sha(base)


def url_key(url: str | None) -> str | None:
    """Canonical URL key: lowercase host without www, path without trailing slash, no query/fragment."""
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}"


def profile_key(full_name: str, company_name: str | None, profile_url: str | None) -> str:
    """Prefer a canonical profile URL; otherwise name + normalized company."""
    ukey = url_key(profile_url)
    if ukey:
        return "pf:" + sha(ukey)
    company = normalize_text(_COMPANY_SUFFIXES.sub(" ", company_name or ""))
    return "pf:" + sha(normalize_text(full_name) + "|" + company)


def action_key(action_type: str, target_id: str | None, payload_fingerprint: str = "") -> str:
    return "ac:" + sha(f"{action_type}|{target_id or '-'}|{payload_fingerprint}")


def content_hash(body: str) -> str:
    return "ct:" + sha(normalize_text(body))


def shingles(body: str, k: int = 3) -> set[str]:
    words = normalize_text(body).split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_semantic_duplicate(body: str, history: list[set[str]], threshold: float = 0.35) -> tuple[bool, float]:
    """Near-duplicate check against prior content shingle sets."""
    current = shingles(body)
    best = max((jaccard(current, h) for h in history), default=0.0)
    return best >= threshold, best
