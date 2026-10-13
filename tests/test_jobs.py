import time
import unittest

from rs_agent import jobs
from rs_agent.errors import AgentError, ErrorCategory

from .helpers import make_db


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def runner(self, handler, **kw):
        return jobs.JobRunner(self.db, {"k": handler}, timeout_s=kw.pop("timeout_s", 2), **kw)

    def test_idempotent_enqueue(self):
        self.assertIsNotNone(jobs.enqueue(self.db, "k", {}, "key-1"))
        self.assertIsNone(jobs.enqueue(self.db, "k", {}, "key-1"))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM jobs"), 1)

    def test_success(self):
        jobs.enqueue(self.db, "k", {"x": 1}, "a")
        seen = []
        self.assertEqual(self.runner(lambda p: seen.append(p)).run_once(), "succeeded")
        self.assertEqual(seen, [{"x": 1}])
        self.assertIsNone(self.runner(lambda p: None).run_once())

    def test_retryable_error_reschedules_with_backoff(self):
        jobs.enqueue(self.db, "k", {}, "b", max_attempts=3)

        def fail(_):
            raise AgentError("503", ErrorCategory.PROVIDER)

        self.assertEqual(self.runner(fail).run_once(), "queued")
        row = self.db.fetchone("SELECT * FROM jobs")
        self.assertEqual((row["attempts"], row["error_category"]), (1, "provider"))
        self.assertGreater(row["run_after"], row["created_at"])
        self.assertIsNone(self.runner(fail).run_once())  # not due yet

    def test_dead_letter_after_max_attempts(self):
        jobs.enqueue(self.db, "k", {}, "c", max_attempts=2)

        def fail(_):
            raise AgentError("429", ErrorCategory.RATE_LIMIT, retry_after_s=0)

        r = self.runner(fail)
        statuses = []
        for _ in range(2):
            self.db.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00+00:00'")
            statuses.append(r.run_once())
        self.assertEqual(statuses, ["queued", "dead"])
        self.assertTrue(jobs.requeue_dead(self.db, self.db.scalar("SELECT id FROM jobs")))

    def test_non_retryable_errors_go_straight_to_dead(self):
        for i, cat in enumerate((ErrorCategory.PERMISSION, ErrorCategory.AUTHENTICATION, ErrorCategory.POLICY,
                                 ErrorCategory.VALIDATION)):
            jobs.enqueue(self.db, "k", {}, f"nr{i}")

            def fail(_, c=cat):
                raise AgentError("no", c)

            self.assertEqual(self.runner(fail).run_once(), "dead")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM errors"), 4)

    def test_unknown_exception_is_internal_and_not_retried(self):
        jobs.enqueue(self.db, "k", {}, "d")
        self.assertEqual(self.runner(lambda p: 1 / 0).run_once(), "dead")
        self.assertEqual(self.db.scalar("SELECT error_category FROM jobs"), "internal")

    def test_timeout(self):
        jobs.enqueue(self.db, "k", {}, "e", max_attempts=1)
        self.assertEqual(self.runner(lambda p: time.sleep(1), timeout_s=0.1).run_once(), "dead")
        self.assertIn("timed out", self.db.scalar("SELECT last_error FROM jobs"))

    def test_missing_handler(self):
        jobs.enqueue(self.db, "other", {}, "f")
        self.assertEqual(self.runner(lambda p: None).run_once(), "dead")

    def test_circuit_breaker_opens_and_defers_without_consuming_attempts(self):
        breaker = jobs.CircuitBreaker(threshold=1, cooldown_s=60)
        jobs.enqueue(self.db, "k", {}, "g1", max_attempts=5)
        jobs.enqueue(self.db, "k", {}, "g2", max_attempts=5)

        def fail(_):
            raise AgentError("down", ErrorCategory.PROVIDER)

        r = self.runner(fail, breakers={"li": breaker}, provider_of={"k": "li"})
        r.run_once()
        self.assertEqual(breaker.state, "open")
        self.db.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00+00:00'")
        r.run_once()
        attempts = sorted(x["attempts"] for x in self.db.fetchall("SELECT attempts FROM jobs"))
        self.assertEqual(attempts, [0, 1])  # deferred job did not burn an attempt

    def test_breaker_half_open_and_reset(self):
        b = jobs.CircuitBreaker(threshold=2, cooldown_s=0.05)
        b.failure()
        b.failure()
        self.assertFalse(b.allow())
        time.sleep(0.06)
        self.assertTrue(b.allow())
        self.assertEqual(b.state, "half_open")
        b.success()
        self.assertEqual(b.state, "closed")

    def test_backoff_bounded(self):
        self.assertEqual(jobs.backoff_seconds(1, jitter=False), 5)
        self.assertEqual(jobs.backoff_seconds(4, jitter=False), 40)
        self.assertEqual(jobs.backoff_seconds(30, jitter=False), 900)

    def test_queue_depth(self):
        jobs.enqueue(self.db, "k", {}, "h")
        self.assertEqual(jobs.queue_depth(self.db), {"queued": 1})


if __name__ == "__main__":
    unittest.main()
