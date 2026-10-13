import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import requests

from rs_agent import approvals, content
from rs_agent.claude import BudgetExceeded, ClaudeClient
from rs_agent.errors import AgentError, ErrorCategory
from rs_agent.executor import execute_action
from rs_agent.jobs import CircuitBreaker
from rs_agent.linkedin import INTROSPECT_URL, POSTS_URL, TOKEN_URL, USERINFO_URL, LinkedInClient, TokenBundle, TokenCipher
from rs_agent.policy import ProposedAction

from .helpers import FERNET_KEY, FakeResponse, FakeSession, grant, make_db, make_settings

TOKEN_OK = {"access_token": "AQtoken", "expires_in": 5184000, "refresh_token": "AQrefresh",
            "refresh_token_expires_in": 31536000, "scope": "openid,profile,w_member_social"}
GOOD_POST = ("A WAREHOUSE CAN HAVE SPACE AND STILL HAVE A FLOW PROBLEM.\n\n" + "Receiving, put-away and picking compete for the "
             "same aisles when staging has no owner. That costs travel, waiting and service. " * 6
             + "\n\nWhere does your operation lose the most time?\n\n#WarehouseManagement #Logistics")


class LinkedInTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.http = FakeSession()
        self.li = LinkedInClient(make_settings(), self.db, self.http)

    def connect(self):
        url = self.li.authorization_url()
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.http.add("POST", TOKEN_URL, FakeResponse(200, TOKEN_OK))
        self.http.add("POST", INTROSPECT_URL, FakeResponse(200, {"active": True, "scope": "openid,profile,w_member_social"}))
        self.http.add("GET", USERINFO_URL, FakeResponse(200, {"sub": "abc123"}))
        return self.li.exchange_code("the-code", state)

    def test_authorization_url_has_state_and_scopes(self):
        q = parse_qs(urlsplit(self.li.authorization_url()).query)
        self.assertEqual(q["response_type"], ["code"])
        self.assertIn("w_member_social", q["scope"][0])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM oauth_states"), 1)

    def test_full_oauth_flow_encrypts_tokens_and_detects_scopes(self):
        bundle = self.connect()
        self.assertEqual(bundle.member_urn, "urn:li:person:abc123")
        row = self.db.fetchone("SELECT * FROM oauth_tokens")
        self.assertNotIn("AQtoken", row["access_token_enc"])
        self.assertEqual(TokenCipher(FERNET_KEY).decrypt(row["access_token_enc"]), "AQtoken")
        granted = {r["scope"] for r in self.db.fetchall("SELECT scope FROM api_permissions WHERE granted = 1")}
        self.assertEqual(granted, {"openid", "profile", "w_member_social"})
        self.assertEqual(self.db.scalar("SELECT granted FROM api_permissions WHERE scope = 'r_member_social'"), 0)
        self.assertEqual(self.li.status()["status"], "HEALTHY")

    def test_state_is_single_use_and_validated(self):
        with self.assertRaises(AgentError) as ctx:
            self.li.exchange_code("code", "forged")
        self.assertEqual(ctx.exception.category, ErrorCategory.AUTHENTICATION)
        self.connect()
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM oauth_states"), 0)

    def test_expired_state(self):
        url = self.li.authorization_url()
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.db.execute("UPDATE oauth_states SET created_at = '2000-01-01T00:00:00+00:00'")
        with self.assertRaises(AgentError):
            self.li.exchange_code("code", state)

    def test_oauth_token_failures(self):
        for status, category in ((400, ErrorCategory.AUTHENTICATION), (401, ErrorCategory.AUTHENTICATION),
                                 (500, ErrorCategory.PROVIDER), (429, ErrorCategory.RATE_LIMIT)):
            with self.subTest(status=status):
                http = FakeSession()
                li = LinkedInClient(make_settings(), self.db, http)
                state = parse_qs(urlsplit(li.authorization_url()).query)["state"][0]
                http.add("POST", TOKEN_URL, FakeResponse(status, {"error": "x"}))
                with self.assertRaises(AgentError) as ctx:
                    li.exchange_code("c", state)
                self.assertEqual(ctx.exception.category, category)

    def test_token_endpoint_network_error_is_temporary(self):
        state = parse_qs(urlsplit(self.li.authorization_url()).query)["state"][0]
        self.http.add("POST", TOKEN_URL, requests.ConnectionError("down"))
        with self.assertRaises(AgentError) as ctx:
            self.li.exchange_code("c", state)
        self.assertEqual(ctx.exception.category, ErrorCategory.TEMPORARY)

    def test_inactive_token_rejected(self):
        state = parse_qs(urlsplit(self.li.authorization_url()).query)["state"][0]
        self.http.add("POST", TOKEN_URL, FakeResponse(200, TOKEN_OK))
        self.http.add("POST", INTROSPECT_URL, FakeResponse(200, {"active": False}))
        with self.assertRaises(AgentError):
            self.li.exchange_code("c", state)

    def test_scope_degradation_without_openid(self):
        state = parse_qs(urlsplit(self.li.authorization_url()).query)["state"][0]
        self.http.add("POST", TOKEN_URL, FakeResponse(200, {**TOKEN_OK, "scope": "w_member_social"}))
        self.http.add("POST", INTROSPECT_URL, FakeResponse(500, None))
        bundle = self.li.exchange_code("c", state)
        self.assertIsNone(bundle.member_urn)
        with self.assertRaises(AgentError) as ctx:
            self.li.create_text_post("hello world")
        self.assertEqual(ctx.exception.category, ErrorCategory.PERMISSION)

    def test_create_post_sends_versioned_headers(self):
        self.connect()
        self.http.add("POST", POSTS_URL, FakeResponse(201, None, {"x-restli-id": "urn:li:share:999"}))
        urn = self.li.create_text_post("Hello operations world")
        self.assertEqual(urn, "urn:li:share:999")
        call = [c for c in self.http.calls if c["url"] == POSTS_URL][0]
        self.assertEqual(call["headers"]["Linkedin-Version"], "202605")
        self.assertEqual(call["headers"]["X-Restli-Protocol-Version"], "2.0.0")
        self.assertEqual(call["json"]["author"], "urn:li:person:abc123")

    def test_provider_errors_are_classified(self):
        self.connect()
        for status, category in ((401, ErrorCategory.AUTHENTICATION), (403, ErrorCategory.PERMISSION),
                                 (429, ErrorCategory.RATE_LIMIT), (503, ErrorCategory.PROVIDER), (422, ErrorCategory.VALIDATION)):
            with self.subTest(status=status):
                self.http.routes[("POST", POSTS_URL)] = [FakeResponse(status, {"message": "x"}, {"Retry-After": "30"})]
                with self.assertRaises(AgentError) as ctx:
                    self.li.create_text_post("hello")
                self.assertEqual(ctx.exception.category, category)
        self.assertEqual(ctx.exception.retry_after_s, 30)

    def test_post_validation(self):
        with self.assertRaises(AgentError):
            self.li.create_text_post("x" * 3001)

    def test_refresh_when_near_expiry(self):
        self.connect()
        soon = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        self.db.execute("UPDATE oauth_tokens SET expires_at = ?", (soon,))
        self.http.routes[("POST", TOKEN_URL)] = [FakeResponse(200, {**TOKEN_OK, "access_token": "AQnew"})]
        self.assertEqual(self.li.access_token(), "AQnew")

    def test_not_connected_and_bad_key(self):
        with self.assertRaises(AgentError):
            self.li.access_token()
        with self.assertRaises(AgentError):
            TokenCipher("not-a-key")
        self.assertEqual(LinkedInClient(make_settings(LINKEDIN_CLIENT_ID=""), self.db).status()["status"], "DEGRADED")

    def test_cipher_accepts_generated_random_secret(self):
        # Render's generateValue yields standard base64 (may contain + and /), not a Fernet key.
        secret = "k+Zq/8vR2mN1pQ7sT4uW6xY9aB3cD5eF0gH2jK4lM6n="
        a, b = TokenCipher(secret), TokenCipher(secret)
        self.assertEqual(b.decrypt(a.encrypt("AQtoken")), "AQtoken")
        with self.assertRaises(AgentError):
            TokenCipher("short+secret/value")

    def test_expired_token_status_blocked(self):
        self.li.save_tokens(TokenBundle("AQx", datetime.now(UTC) - timedelta(days=1), scopes=("w_member_social",)))
        self.assertEqual(self.li.status()["status"], "BLOCKED")


def claude_ok(text="Draft text", inp=1000, out=500):
    return FakeResponse(200, {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": inp, "output_tokens": out}})


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.http = FakeSession()
        self.settings = make_settings()
        self.claude = ClaudeClient(self.settings, self.db, self.http, CircuitBreaker(threshold=2, cooldown_s=60))

    def test_routing(self):
        self.assertEqual(self.claude.model_for("complex"), self.settings.claude_model_complex)
        self.assertEqual(self.claude.model_for("simple"), self.settings.claude_model_simple)

    def test_complete_records_usage_and_cost(self):
        self.http.add("POST", "https://api.anthropic.com", claude_ok())
        r = self.claude.complete("sys", "user", complexity="complex", purpose="post_draft")
        self.assertEqual(r.text, "Draft text")
        self.assertEqual(r.cost_usd, Decimal("0.0525"))  # 1000*15/1e6 + 500*75/1e6
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM llm_usage"), 1)
        body = json.loads(self.http.calls[0]["data"])
        self.assertEqual(body["model"], self.settings.claude_model_complex)

    def test_budget_blocks_calls(self):
        s = make_settings(DAILY_BUDGET_USD="0.01")
        c = ClaudeClient(s, self.db, self.http)
        self.http.add("POST", "https://api.anthropic.com", claude_ok())
        c.complete("s", "u", complexity="complex")
        with self.assertRaises(BudgetExceeded):
            c.complete("s", "u")
        self.assertEqual(c.status()["status"], "BLOCKED")

    def test_errors_and_circuit_breaker(self):
        self.http.add("POST", "https://api.anthropic.com", FakeResponse(529, {"error": "overloaded"}))
        for _ in range(2):
            with self.assertRaises(AgentError) as ctx:
                self.claude.complete("s", "u")
            self.assertEqual(ctx.exception.category, ErrorCategory.PROVIDER)
        with self.assertRaises(AgentError) as ctx:
            self.claude.complete("s", "u")
        self.assertIn("circuit", str(ctx.exception))
        self.assertEqual(self.claude.status()["status"], "FAILED")

    def test_auth_error_does_not_trip_breaker(self):
        self.http.add("POST", "https://api.anthropic.com", FakeResponse(401, {}))
        for _ in range(3):
            with self.assertRaises(AgentError) as ctx:
                self.claude.complete("s", "u")
            self.assertEqual(ctx.exception.category, ErrorCategory.AUTHENTICATION)
        self.assertEqual(self.claude.breaker.state, "closed")

    def test_malformed_and_empty_responses(self):
        self.http.add("POST", "https://api.anthropic.com", FakeResponse(200, {"content": []}))
        with self.assertRaises(AgentError):
            self.claude.complete("s", "u")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM llm_usage"), 0)

    def test_network_error_is_temporary(self):
        self.http.add("POST", "https://api.anthropic.com", requests.Timeout("slow"))
        with self.assertRaises(AgentError) as ctx:
            self.claude.complete("s", "u")
        self.assertEqual(ctx.exception.category, ErrorCategory.TEMPORARY)

    def test_not_configured(self):
        c = ClaudeClient(make_settings(ANTHROPIC_API_KEY=""), self.db, self.http)
        self.assertEqual(c.status()["status"], "DEGRADED")
        with self.assertRaises(AgentError):
            c.complete("s", "u")


class ContentAndExecutorTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.http = FakeSession()
        self.settings = make_settings()
        self.claude = ClaudeClient(self.settings, self.db, self.http)
        grant(self.db, "w_member_social")

    def test_lint_rules(self):
        self.assertEqual(content.lint_post(GOOD_POST), [])
        self.assertTrue(any("banned" in p for p in content.lint_post(GOOD_POST + " This is a game changer.")))
        self.assertTrue(any("percentage" in p for p in content.lint_post(GOOD_POST + " Costs drop 30%.")))
        self.assertEqual(content.lint_post(GOOD_POST + " Costs drop 30% (illustrative)."), [])
        self.assertTrue(any("dollar" in p for p in content.lint_post(GOOD_POST + " Saved $50,000.")))
        self.assertTrue(any("short" in p for p in content.lint_post("Too short")))

    def test_generate_post_goes_to_approval_queue(self):
        self.http.add("POST", "https://api.anthropic.com", claude_ok(GOOD_POST))
        result = content.generate_post(self.db, self.settings, self.claude, "Warehouse", "staging ownership")
        self.assertEqual(result.decision.state.value, "MANUAL_APPROVAL_REQUIRED")
        self.assertEqual(self.db.scalar("SELECT status FROM content"), "queued")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM jobs"), 0)

    def test_duplicate_content_rejected(self):
        content.save_draft(self.db, GOOD_POST, "Warehouse", None)
        with self.assertRaises(AgentError):
            content.save_draft(self.db, GOOD_POST, "Warehouse", None)
        with self.assertRaises(AgentError) as ctx:
            content.save_draft(self.db, GOOD_POST.replace("Where does", "What part of"), "Warehouse", None)
        self.assertIn("similar", str(ctx.exception))

    def test_bad_draft_leaves_no_data(self):
        self.http.add("POST", "https://api.anthropic.com", claude_ok(GOOD_POST + " It's a game changer."))
        with self.assertRaises(AgentError):
            content.generate_post(self.db, self.settings, self.claude, "Warehouse", "x")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM content"), 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions"), 0)

    def test_ai_failure_does_not_corrupt_data(self):
        self.http.add("POST", "https://api.anthropic.com", FakeResponse(500, {}))
        with self.assertRaises(AgentError):
            content.generate_post(self.db, self.settings, self.claude, "Fleet", "x")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM content"), 0)

    def test_unknown_topic(self):
        with self.assertRaises(AgentError):
            content.generate_post(self.db, self.settings, self.claude, "Cooking", "x")

    def _connected_li(self):
        li = LinkedInClient(self.settings, self.db, self.http)
        li.save_tokens(TokenBundle("AQtok", datetime.now(UTC) + timedelta(days=30), scopes=("w_member_social",),
                                   member_urn="urn:li:person:me"))
        return li

    def test_execute_approved_post_is_idempotent(self):
        li = self._connected_li()
        cid = content.save_draft(self.db, GOOD_POST, "Warehouse", None)
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", f"content:{cid}", "Post", GOOD_POST))
        approvals.decide(self.db, self.settings, r.queue_id, "approve", "t")
        self.http.add("POST", POSTS_URL, FakeResponse(201, None, {"x-restli-id": "urn:li:share:1"}))
        self.assertEqual(execute_action(self.db, li, r.action_id)["external_ref"], "urn:li:share:1")
        again = execute_action(self.db, li, r.action_id)
        self.assertTrue(again["idempotent"])
        self.assertEqual(len([c for c in self.http.calls if c["url"] == POSTS_URL]), 1)
        self.assertEqual(self.db.scalar("SELECT status FROM content"), "published")
        self.assertEqual(self.db.scalar("SELECT linkedin_urn FROM posts"), "urn:li:share:1")

    def test_regression_missing_content_row_does_not_fail_published_post(self):
        # Bug found 2026-10-12: FK error on posts insert after LinkedIn had already accepted the post.
        li = self._connected_li()
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:ghost", "Post", GOOD_POST))
        approvals.decide(self.db, self.settings, r.queue_id, "approve", "t")
        self.http.add("POST", POSTS_URL, FakeResponse(201, None, {"x-restli-id": "urn:li:share:2"}))
        self.assertEqual(execute_action(self.db, li, r.action_id)["status"], "executed")
        self.assertEqual(self.db.scalar("SELECT status FROM actions WHERE id = ?", (r.action_id,)), "executed")

    def test_execute_refuses_unapproved_or_manual(self):
        li = self._connected_li()
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:x", "Post", GOOD_POST))
        with self.assertRaises(AgentError) as ctx:
            execute_action(self.db, li, r.action_id)
        self.assertEqual(ctx.exception.category, ErrorCategory.POLICY)
        with self.assertRaises(AgentError):
            execute_action(self.db, li, "missing")

    def test_execute_rechecks_scope(self):
        li = self._connected_li()
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:y", "Post", GOOD_POST))
        approvals.decide(self.db, self.settings, r.queue_id, "approve", "t")
        self.db.execute("UPDATE api_permissions SET granted = 0")
        with self.assertRaises(AgentError) as ctx:
            execute_action(self.db, li, r.action_id)
        self.assertEqual(ctx.exception.category, ErrorCategory.PERMISSION)


if __name__ == "__main__":
    unittest.main()
