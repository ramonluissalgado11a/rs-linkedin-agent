"""Test helpers: in-memory DB, settings and a fake HTTP session (no network)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import requests
from cryptography.fernet import Fernet

from rs_agent.app_context import AppContext, build_context, ensure_source
from rs_agent.config import Settings, load_settings
from rs_agent.db import Database
from rs_agent.qualification import Candidate

FERNET_KEY = Fernet.generate_key().decode()
ADMIN = "a" * 40


def make_settings(**overrides: str) -> Settings:
    env = {
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "ADMIN_TOKEN": ADMIN,
        "SESSION_SECRET": "s" * 40,
        "TOKEN_ENCRYPTION_KEY": FERNET_KEY,
        "LINKEDIN_CLIENT_ID": "cid",
        "LINKEDIN_CLIENT_SECRET": "csecret-value",
        "LINKEDIN_REDIRECT_URI": "http://localhost:8000/oauth/linkedin/callback",
        "ANTHROPIC_API_KEY": "sk-ant-test-key",
        "WORKER_POLL_S": "0",
    }
    env.update(overrides)
    return load_settings(env)


def make_db() -> Database:
    db = Database("sqlite:///:memory:")
    db.migrate()
    return db


def make_ctx(**overrides: str) -> AppContext:
    settings = make_settings(**overrides)
    return build_context(settings, make_db(), strict=False)


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession(requests.Session):
    """Routes requests by (method, url-prefix) to queued responses or exceptions."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[tuple[str, str], list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url: str, *responses: Any) -> None:
        self.routes.setdefault((method.upper(), url), []).extend(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls.append({"method": method.upper(), "url": url, **kwargs})
        for (m, prefix), queue in self.routes.items():
            if m == method.upper() and url.startswith(prefix) and queue:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"unexpected request {method} {url}")

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any) -> Any:  # type: ignore[override]
        return self.request("POST", url, data=data, json=json, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
        return self.request("GET", url, **kwargs)


def source(db: Database) -> str:
    return ensure_source(db, "public_web", "Test source", "https://example.org/news")


def good_candidate(db: Database, name: str = "Maria Rivera", company: str = "Caribe Logistics Group",
                   url: str | None = "https://www.linkedin.com/in/maria-rivera-test/") -> Candidate:
    return Candidate(
        full_name=name, source_id=source(db), source_url="https://example.org/news/maria",
        role_title="Director of Warehouse Operations", company_name=company, industry="Logistics",
        region="Puerto Rico", headline="Warehouse and distribution leader", profile_url=url,
        evidence_date=(date.today() - timedelta(days=30)).isoformat(),
    )


def grant(db: Database, *scopes: str) -> None:
    from rs_agent.db import utcnow
    from rs_agent.logging_setup import new_id
    for s in scopes:
        db.execute("INSERT INTO api_permissions (id, provider, scope, granted, checked_at) VALUES (?, 'linkedin', ?, 1, ?)",
                   (new_id(), s, utcnow()))
