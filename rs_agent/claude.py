"""Claude integration: model routing, cost accounting, budgets and a circuit breaker.

Calls the Anthropic Messages API over HTTPS. Model IDs and prices come from
configuration so nothing goes stale in code.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import requests

from .config import Settings
from .db import Database, utcnow
from .errors import AgentError, ErrorCategory, classify_http_status
from .jobs import CircuitBreaker
from .logging_setup import new_id

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


@dataclass
class ClaudeResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class BudgetExceeded(AgentError):
    category = ErrorCategory.POLICY


class ClaudeClient:
    def __init__(self, settings: Settings, db: Database, session: requests.Session | None = None,
                 breaker: CircuitBreaker | None = None) -> None:
        self.settings = settings
        self.db = db
        self.http = session or requests.Session()
        self.breaker = breaker or CircuitBreaker(threshold=4, cooldown_s=300)

    def model_for(self, complexity: str) -> str:
        """Route complex reasoning to the most capable model, simple/high-volume work to the fast one."""
        return self.settings.claude_model_complex if complexity == "complex" else self.settings.claude_model_simple

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        price = self.settings.model_prices.get(model)
        if price is None:
            return Decimal("0")
        return (Decimal(input_tokens) * price.input_per_mtok + Decimal(output_tokens) * price.output_per_mtok) / Decimal(1_000_000)

    def spend(self) -> dict[str, Decimal]:
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        day = Decimal(str(self.db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE created_at >= ?", (day_start,)) or 0))
        month = Decimal(str(self.db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE created_at >= ?", (month_start,)) or 0))
        days_elapsed = max(now.day, 1)
        return {"daily": day, "monthly": month, "monthly_estimate": (month / days_elapsed * 30).quantize(Decimal("0.01"))}

    def check_budget(self) -> None:
        s = self.spend()
        if s["daily"] >= self.settings.daily_budget_usd:
            raise BudgetExceeded(f"daily AI budget reached (${s['daily']:.2f})")
        if s["monthly"] >= self.settings.monthly_budget_usd:
            raise BudgetExceeded(f"monthly AI budget reached (${s['monthly']:.2f})")

    def status(self) -> dict[str, Any]:
        if not self.settings.claude_configured:
            return {"status": "DEGRADED", "detail": "ANTHROPIC_API_KEY not set"}
        s = self.spend()
        state = "HEALTHY"
        if self.breaker.state == "open":
            state = "FAILED"
        elif s["daily"] >= self.settings.daily_budget_usd or s["monthly"] >= self.settings.monthly_budget_usd:
            state = "BLOCKED"
        return {"status": state, "circuit": self.breaker.state, "daily_cost_usd": float(s["daily"]),
                "monthly_cost_usd": float(s["monthly"]), "monthly_estimate_usd": float(s["monthly_estimate"])}

    def complete(self, system: str, user: str, complexity: str = "simple", max_tokens: int = 1200,
                 purpose: str = "general", subject_kind: str | None = None, subject_id: str | None = None) -> ClaudeResult:
        if not self.settings.claude_configured:
            raise AgentError("Claude is not configured", ErrorCategory.VALIDATION)
        if not self.breaker.allow():
            raise AgentError("Claude circuit breaker is open", ErrorCategory.PROVIDER, retry_after_s=int(self.breaker.cooldown_s))
        self.check_budget()
        model = self.model_for(complexity)
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.settings.anthropic_api_key or "", "anthropic-version": API_VERSION,
                   "content-type": "application/json"}
        started = time.monotonic()
        try:
            resp = self.http.post(API_URL, headers=headers, data=json.dumps(body), timeout=self.settings.claude_timeout_s)
        except requests.RequestException as exc:
            self.breaker.failure()
            raise AgentError(f"Claude unreachable: {type(exc).__name__}", ErrorCategory.TEMPORARY) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            category = classify_http_status(resp.status_code)
            if resp.status_code == 529:
                category = ErrorCategory.PROVIDER
            if category in (ErrorCategory.PROVIDER, ErrorCategory.TEMPORARY):
                self.breaker.failure()
            retry_after = resp.headers.get("retry-after")
            raise AgentError(f"Claude API failed ({resp.status_code})", category,
                             retry_after_s=int(retry_after) if retry_after and retry_after.isdigit() else None)
        try:
            data = resp.json()
            text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
            usage = data.get("usage", {})
            in_tok, out_tok = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        except (ValueError, AttributeError, TypeError) as exc:
            self.breaker.failure()
            raise AgentError("Claude returned an unexpected response", ErrorCategory.PROVIDER) from exc
        if not text.strip():
            raise AgentError("Claude returned empty text", ErrorCategory.PROVIDER)
        self.breaker.success()
        cost = self.cost(model, in_tok, out_tok)
        self.db.execute(
            "INSERT INTO llm_usage (id, model, purpose, input_tokens, output_tokens, cost_usd, subject_kind, subject_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), model, purpose, in_tok, out_tok, float(cost), subject_kind, subject_id, utcnow()),
        )
        self.db.execute(
            "INSERT INTO metrics (id, name, value, labels, created_at) VALUES (?, 'claude_latency_ms', ?, ?, ?)",
            (new_id(), latency_ms, json.dumps({"model": model}), utcnow()),
        )
        return ClaudeResult(text.strip(), model, in_tok, out_tok, cost)
