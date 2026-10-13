import re
import unittest
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient

from rs_agent import approvals
from rs_agent.linkedin import POSTS_URL, TokenBundle
from rs_agent.policy import ProposedAction
from rs_agent.worker import build_runner, run_forever

from .helpers import ADMIN, FakeResponse, FakeSession, good_candidate, grant, make_ctx

AUTH = {"Authorization": f"Bearer {ADMIN}"}
POST = "Dock-to-stock is the clock nobody watches. " * 12


class WebTests(unittest.TestCase):
    def setUp(self):
        from rs_agent.web import create_app
        self.ctx = make_ctx()
        self.client = TestClient(create_app(self.ctx))

    def login(self):
        r = self.client.post("/login", data={"token": ADMIN}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        page = self.client.get("/")
        return re.search(r'name="csrf" value="([^"]+)"', page.text)

    def test_health_and_ready(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertTrue(self.client.get("/ready").json()["ready"])
        self.assertIn("x-correlation-id", self.client.get("/health").headers)

    def test_ready_fails_when_db_down(self):
        self.ctx.db.close()
        self.assertEqual(self.client.get("/ready").status_code, 503)

    def test_auth_required(self):
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.client.get("/api/dashboard", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.post("/login", data={"token": "nope"}).status_code, 401)

    def test_dashboard_shows_statuses(self):
        self.login()
        page = self.client.get("/").text
        for label in ("System", "LinkedIn API", "Claude", "Database", "Queue", "Qualification %", "Pending approvals"):
            self.assertIn(label, page)
        data = self.client.get("/api/dashboard", headers=AUTH).json()
        self.assertEqual(data["health"]["linkedin"]["status"], "DEGRADED")  # not connected yet
        self.assertEqual(data["health"]["system"], "DEGRADED")

    def test_candidate_ingest_requires_authorized_source(self):
        bad = self.client.post("/api/candidates", headers=AUTH, json={"source": {"kind": "linkedin_scrape", "name": "x"},
                                                                     "candidates": [{"full_name": "A B"}]})
        self.assertEqual(bad.status_code, 400)
        c = good_candidate(self.ctx.db)
        ok = self.client.post("/api/candidates", headers=AUTH, json={
            "source": {"kind": "public_web", "name": "Port news", "url": "https://example.org"},
            "candidates": [{**c.__dict__, "source_id": None}]})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["results"][0]["qualified"])
        self.assertEqual(self.client.post("/api/candidates", headers=AUTH, json={"source": {"kind": "public_web", "name": "x"},
                                                                                "candidates": []}).status_code, 400)

    def test_propose_and_decide_via_api(self):
        prof = self.client.post("/api/candidates", headers=AUTH, json={
            "source": {"kind": "referral", "name": "Colleague"},
            "candidates": [{**good_candidate(self.ctx.db).__dict__, "source_id": None}]}).json()["results"][0]
        r = self.client.post("/api/actions", headers=AUTH, json={"action_type": "CONNECT", "target_kind": "profile",
                                                                 "target_id": prof["profile_id"], "content": "Hi Maria"}).json()
        self.assertEqual(r["state"], "MANUAL_ACTION_REQUIRED")
        self.assertEqual(len(self.client.get("/api/queue", headers=AUTH).json()["items"]), 1)
        d = self.client.post(f"/api/queue/{r['queue_id']}", headers=AUTH, json={"decision": "skip"})
        self.assertEqual(d.json()["status"], "skipped")
        self.assertEqual(self.client.post(f"/api/queue/{r['queue_id']}", headers=AUTH, json={"decision": "approve"}).status_code, 400)
        self.assertEqual(self.client.post("/api/actions", headers=AUTH, json={"action_type": "SCRAPE"}).status_code, 400)

    def test_dashboard_form_requires_csrf(self):
        grant(self.ctx.db, "w_member_social")
        res = approvals.propose(self.ctx.db, self.ctx.settings, ProposedAction("CREATE_POST", "self", "content:1", "Post", POST))
        m = self.login()
        bad = self.client.post(f"/queue/{res.queue_id}", data={"csrf": "x", "decision": "approve"}, follow_redirects=False)
        self.assertEqual(bad.status_code, 403)
        ok = self.client.post(f"/queue/{res.queue_id}", data={"csrf": m.group(1), "decision": "approve", "content": POST},
                              follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(self.ctx.db.scalar("SELECT status FROM actions"), "approved")

    def test_approve_with_edit_from_dashboard(self):
        grant(self.ctx.db, "w_member_social")
        res = approvals.propose(self.ctx.db, self.ctx.settings, ProposedAction("CREATE_POST", "self", "content:2", "Post", POST))
        m = self.login()
        edited = "Edited by Ramon. " * 20
        self.client.post(f"/queue/{res.queue_id}", data={"csrf": m.group(1), "decision": "approve", "content": edited})
        self.assertIn("Edited by Ramon", self.ctx.db.scalar("SELECT payload FROM actions"))

    def test_html_is_escaped(self):
        grant(self.ctx.db, "w_member_social")
        approvals.propose(self.ctx.db, self.ctx.settings, ProposedAction(
            "CREATE_POST", "self", "content:3", "<script>alert(1)</script>", POST))
        self.login()
        page = self.client.get("/").text
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_oauth_callback_errors(self):
        self.assertEqual(self.client.get("/oauth/linkedin/callback?error=user_cancelled").status_code, 400)
        self.assertEqual(self.client.get("/oauth/linkedin/callback?code=x&state=forged").status_code, 400)
        self.assertGreaterEqual(self.ctx.db.scalar("SELECT COUNT(*) FROM errors WHERE category = 'authentication'"), 2)

    def test_oauth_start_requires_login(self):
        self.assertEqual(self.client.get("/oauth/linkedin/start", follow_redirects=False).status_code, 303)
        self.login()
        r = self.client.get("/oauth/linkedin/start", follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("https://www.linkedin.com/oauth/v2/authorization"))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.http = FakeSession()
        self.ctx.linkedin.http = self.http
        grant(self.ctx.db, "w_member_social")
        self.ctx.linkedin.save_tokens(TokenBundle("AQtok", datetime.now(UTC) + timedelta(days=30),
                                                  scopes=("w_member_social",), member_urn="urn:li:person:me"))

    def approved_post(self, target="content:w1"):
        r = approvals.propose(self.ctx.db, self.ctx.settings, ProposedAction("CREATE_POST", "self", target, "Post", POST))
        approvals.decide(self.ctx.db, self.ctx.settings, r.queue_id, "approve", "t")
        return r.action_id

    def test_worker_executes_approved_post(self):
        action_id = self.approved_post()
        self.http.add("POST", POSTS_URL, FakeResponse(201, None, {"x-restli-id": "urn:li:share:7"}))
        run_forever(self.ctx, max_iterations=2)
        self.assertEqual(self.ctx.db.scalar("SELECT status FROM actions WHERE id = ?", (action_id,)), "executed")
        self.assertEqual(self.ctx.db.scalar("SELECT status FROM system_runs"), "stopped")

    def test_linkedin_outage_retries_and_worker_survives(self):
        action_id = self.approved_post()
        self.http.add("POST", POSTS_URL, FakeResponse(503, {}))
        runner = build_runner(self.ctx)
        self.assertEqual(runner.run_once(), "queued")
        self.assertEqual(self.ctx.db.scalar("SELECT status FROM actions WHERE id = ?", (action_id,)), "approved")
        runner.shutdown()

    def test_permission_error_marks_action_failed(self):
        action_id = self.approved_post()
        self.http.add("POST", POSTS_URL, FakeResponse(403, {}))
        runner = build_runner(self.ctx)
        self.assertEqual(runner.run_once(), "dead")
        self.assertEqual(self.ctx.db.scalar("SELECT status FROM actions WHERE id = ?", (action_id,)), "failed")
        runner.shutdown()


if __name__ == "__main__":
    unittest.main()
