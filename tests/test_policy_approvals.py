import unittest
from dataclasses import replace

from rs_agent import approvals
from rs_agent.errors import AgentError
from rs_agent.policy import PolicyState, ProposedAction, evaluate
from rs_agent.qualification import qualify

from .helpers import good_candidate, grant, make_db, make_settings

POST = "Inventory accuracy is built into every transaction. " * 10


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.settings = make_settings()

    def post(self, text=POST, target="content:1"):
        return ProposedAction("CREATE_POST", "self", target, "Post", text)

    def test_forbidden_and_unknown_actions_blocked(self):
        for t in ("SCRAPE", "BULK_FOLLOW", "EVADE_DETECTION", "BROWSER_AUTOMATION"):
            self.assertEqual(evaluate(self.db, self.settings, ProposedAction(t)).state, PolicyState.BLOCKED_POLICY)
        d = evaluate(self.db, self.settings, ProposedAction("TELEPORT"))
        self.assertEqual((d.state, d.reasons), (PolicyState.BLOCKED_POLICY, ["UNKNOWN_ACTION"]))

    def test_post_without_scope_is_blocked_permission(self):
        d = evaluate(self.db, self.settings, self.post())
        self.assertEqual(d.state, PolicyState.BLOCKED_PERMISSION)
        self.assertIn("MISSING_SCOPE:w_member_social", d.reasons)

    def test_post_with_scope_requires_manual_approval_by_default(self):
        grant(self.db, "w_member_social")
        d = evaluate(self.db, self.settings, self.post())
        self.assertEqual(d.state, PolicyState.MANUAL_APPROVAL_REQUIRED)
        self.assertEqual(d.permission_used, "w_member_social")

    def test_auto_publish_only_when_explicitly_enabled(self):
        grant(self.db, "w_member_social")
        d = evaluate(self.db, make_settings(LINKEDIN_AUTO_PUBLISH="true"), self.post())
        self.assertEqual(d.state, PolicyState.APPROVED_AUTOMATION)

    def test_member_interactions_are_always_manual(self):
        grant(self.db, "w_member_social", "w_member_social_feed", "r_member_social")
        prof = qualify(self.db, good_candidate(self.db))
        for t in ("FOLLOW", "CONNECT", "REACT", "MESSAGE", "COMMENT"):
            with self.subTest(t=t):
                d = evaluate(self.db, self.settings, ProposedAction(t, "profile", prof.profile_id, "Maria", "note"))
                self.assertEqual(d.state, PolicyState.MANUAL_ACTION_REQUIRED)
                self.assertIn("NO_AUTHORIZED_API", d.reasons)
        d = evaluate(self.db, self.settings, ProposedAction("COMMENT", "profile", prof.profile_id, "Maria", "x"))
        self.assertTrue(d.human_response_required)
        self.assertIn("HUMAN_RESPONSE_REQUIRED", d.reasons)

    def test_target_must_exist_and_be_qualified(self):
        self.assertIn("MISSING_TARGET", evaluate(self.db, self.settings, ProposedAction("FOLLOW")).reasons)
        rejected = qualify(self.db, replace(good_candidate(self.db), role_title=None))
        d = evaluate(self.db, self.settings, ProposedAction("FOLLOW", "profile", rejected.profile_id))
        self.assertIn("TARGET_NOT_QUALIFIED", d.reasons)

    def test_content_length_gate(self):
        grant(self.db, "w_member_social")
        self.assertIn("CONTENT_TOO_LONG", evaluate(self.db, self.settings, self.post("x" * 3001)).reasons)

    def test_rate_limit_blocks_second_post_same_hour(self):
        grant(self.db, "w_member_social")
        s = make_settings(POSTS_PER_DAY="2")
        r1 = approvals.propose(self.db, s, self.post(POST, "content:1"))
        self.assertEqual(r1.decision.state, PolicyState.MANUAL_APPROVAL_REQUIRED)
        r2 = approvals.propose(self.db, s, self.post(POST + " different", "content:2"))
        self.assertEqual(r2.decision.state, PolicyState.BLOCKED_RATE_LIMIT)

    def test_duplicate_action_and_recent_action(self):
        grant(self.db, "w_member_social")
        prof = qualify(self.db, good_candidate(self.db))
        a = approvals.propose(self.db, self.settings, ProposedAction("FOLLOW", "profile", prof.profile_id, "Maria"))
        self.assertIsNotNone(a.action_id)
        dup = approvals.propose(self.db, self.settings, ProposedAction("FOLLOW", "profile", prof.profile_id, "Maria"))
        self.assertEqual(dup.decision.state, PolicyState.BLOCKED_DUPLICATE)
        self.assertIsNone(dup.action_id)
        recent = approvals.propose(self.db, self.settings,
                                   ProposedAction("FOLLOW", "profile", prof.profile_id, "Maria", "with note"))
        self.assertIn("RECENT_ACTION_SAME_TARGET", recent.decision.reasons)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions"), 1)


class ApprovalQueueTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.settings = make_settings()
        grant(self.db, "w_member_social")

    def test_pending_items_never_create_jobs_until_approved(self):
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:1", "Post", POST))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM jobs"), 0)
        items = approvals.list_queue(self.db)
        self.assertEqual(len(items), 1)
        for field in ("action_type", "target_label", "reason", "proposed_content", "risk", "permission_used", "state"):
            self.assertIn(field, items[0])
        approvals.decide(self.db, self.settings, r.queue_id, "approve", "tester")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'execute_action'"), 1)

    def test_manual_action_approve_never_executes(self):
        prof = qualify(self.db, good_candidate(self.db))
        r = approvals.propose(self.db, self.settings, ProposedAction("CONNECT", "profile", prof.profile_id, "Maria", "Hi"))
        approvals.decide(self.db, self.settings, r.queue_id, "approve", "tester")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM jobs"), 0)
        self.assertEqual(self.db.scalar("SELECT status FROM actions WHERE id = ?", (r.action_id,)), "manual_pending")
        approvals.decide(self.db, self.settings, r.queue_id, "mark_done", "tester")
        self.assertEqual(self.db.scalar("SELECT status FROM actions WHERE id = ?", (r.action_id,)), "executed")

    def test_edit_reject_skip_and_audit(self):
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:9", "Post", POST))
        approvals.decide(self.db, self.settings, r.queue_id, "edit", "tester", "Edited body " * 30)
        self.assertTrue(approvals.list_queue(self.db)[0]["edited_content"].startswith("Edited body"))
        approvals.decide(self.db, self.settings, r.queue_id, "reject", "tester")
        with self.assertRaises(AgentError):
            approvals.decide(self.db, self.settings, r.queue_id, "approve", "tester")
        events = [e["event"] for e in self.db.fetchall("SELECT event FROM audit_log ORDER BY created_at")]
        self.assertIn("queue_edit", events)
        self.assertIn("queue_reject", events)

    def test_invalid_decision_and_bad_edit(self):
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:3", "Post", POST))
        with self.assertRaises(AgentError):
            approvals.decide(self.db, self.settings, r.queue_id, "explode", "t")
        with self.assertRaises(AgentError):
            approvals.decide(self.db, self.settings, r.queue_id, "edit", "t", "x" * 3001)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM approvals"), 0)  # rolled back
        with self.assertRaises(AgentError):
            approvals.decide(self.db, self.settings, "missing", "approve", "t")

    def test_mark_done_only_for_manual(self):
        r = approvals.propose(self.db, self.settings, ProposedAction("CREATE_POST", "self", "content:4", "Post", POST))
        with self.assertRaises(AgentError):
            approvals.decide(self.db, self.settings, r.queue_id, "mark_done", "t")


if __name__ == "__main__":
    unittest.main()
