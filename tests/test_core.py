import json
import logging
import unittest

from rs_agent import dedup
from rs_agent.config import load_settings, validate_settings
from rs_agent.db import Database, DatabaseError
from rs_agent.logging_setup import JsonFormatter, redact, redact_text

from .helpers import make_db, make_settings


class ConfigTests(unittest.TestCase):
    def test_defaults_and_centralized_versions(self):
        s = load_settings({})
        self.assertEqual(s.linkedin_api_version, "202605")
        self.assertTrue(s.claude_model_complex and s.claude_model_simple)
        self.assertFalse(s.linkedin_auto_publish)

    def test_validation_flags_weak_secrets(self):
        problems = validate_settings(load_settings({"ADMIN_TOKEN": "short", "SESSION_SECRET": "x"}))
        self.assertTrue(any("ADMIN_TOKEN" in p for p in problems))
        self.assertTrue(any("SESSION_SECRET" in p for p in problems))

    def test_validation_requires_encryption_key_with_oauth(self):
        s = make_settings(TOKEN_ENCRYPTION_KEY="")
        self.assertIn("TOKEN_ENCRYPTION_KEY is required when LinkedIn OAuth is configured", validate_settings(s))

    def test_production_requires_https(self):
        s = make_settings(APP_ENV="production", PUBLIC_BASE_URL="http://x.example")
        self.assertTrue(any("https" in p for p in validate_settings(s)))

    def test_bad_version_rejected(self):
        self.assertTrue(any("YYYYMM" in p for p in validate_settings(make_settings(LINKEDIN_API_VERSION="v2"))))

    def test_rate_limits_never_exceed_hard_ceiling(self):
        s = make_settings(POSTS_PER_DAY="50")
        self.assertEqual(s.rate_limit("CREATE_POST"), (1, 2))
        self.assertEqual(s.rate_limit("FOLLOW"), (0, 0))

    def test_strict_startup_blocks_bad_config(self):
        from rs_agent.app_context import build_context
        with self.assertRaises(SystemExit):
            build_context(make_settings(ADMIN_TOKEN="short"), make_db(), strict=True)


class LoggingTests(unittest.TestCase):
    def test_redacts_sensitive_keys_recursively(self):
        out = redact({"access_token": "AQabc", "nested": {"client_secret": "x", "ok": "fine"}, "list": [{"password": "p"}]})
        self.assertEqual(out["access_token"], "[REDACTED]")
        self.assertEqual(out["nested"]["client_secret"], "[REDACTED]")
        self.assertEqual(out["nested"]["ok"], "fine")
        self.assertEqual(out["list"][0]["password"], "[REDACTED]")

    def test_redacts_tokens_in_free_text(self):
        text = redact_text("Authorization: Bearer AQX123abc and sk-ant-api03-zzz and code=abc123&state=1")
        self.assertNotIn("AQX123abc", text)
        self.assertNotIn("sk-ant-api03-zzz", text)
        self.assertNotIn("abc123", text)

    def test_formatter_never_prints_secrets(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "token=%s", ("Bearer AQsecretvalue",), None)
        record.fields = {"refresh_token": "AQrefresh", "count": 3}
        line = JsonFormatter().format(record)
        self.assertNotIn("AQsecretvalue", line)
        self.assertNotIn("AQrefresh", line)
        self.assertEqual(json.loads(line)["count"], 3)


class DedupTests(unittest.TestCase):
    def test_company_key_ignores_suffixes_and_case(self):
        self.assertEqual(dedup.company_key("Caribe Logistics, Inc."), dedup.company_key("caribe logistics LLC"))

    def test_company_key_prefers_domain(self):
        self.assertEqual(dedup.company_key("A", "https://www.caribe.com/about"), dedup.company_key("B", "caribe.com"))

    def test_url_key_canonicalizes(self):
        self.assertEqual(dedup.url_key("https://www.LinkedIn.com/in/Jane-Doe/?trk=x"), dedup.url_key("linkedin.com/in/jane-doe"))
        self.assertIsNone(dedup.url_key(""))

    def test_profile_key_by_url_or_name_company(self):
        a = dedup.profile_key("José Pérez", "Acme Inc", None)
        b = dedup.profile_key("Jose Perez", "ACME", None)
        self.assertEqual(a, b)
        self.assertNotEqual(a, dedup.profile_key("Jose Perez", "Other Co", None))

    def test_content_hash_ignores_whitespace_and_punctuation(self):
        self.assertEqual(dedup.content_hash("Hello,   world!"), dedup.content_hash("hello world"))

    def test_semantic_duplicate_detection(self):
        base = "warehouse layout drives travel distance and labor productivity across receiving picking and shipping"
        hist = [dedup.shingles(base)]
        dup, score = dedup.is_semantic_duplicate(base + " every day", hist)
        self.assertTrue(dup)
        self.assertGreater(score, 0.5)
        self.assertFalse(dedup.is_semantic_duplicate("fleet preventive maintenance reduces downtime and cost", hist)[0])


class DatabaseTests(unittest.TestCase):
    def test_migrations_idempotent(self):
        db = Database("sqlite:///:memory:")
        self.assertEqual(db.migrate(), ["0001_initial.sql"])
        self.assertEqual(db.migrate(), [])

    def test_transaction_rolls_back(self):
        db = make_db()
        with self.assertRaises(RuntimeError), db.transaction():
            db.execute("INSERT INTO metrics (id, name, value, created_at) VALUES ('1', 'x', 1, 'now')")
            raise RuntimeError("boom")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM metrics"), 0)

    def test_nested_transaction_inner_rollback_keeps_outer(self):
        db = make_db()
        with db.transaction():
            db.execute("INSERT INTO metrics (id, name, value, created_at) VALUES ('1', 'outer', 1, 'now')")
            with self.assertRaises(ValueError), db.transaction():
                db.execute("INSERT INTO metrics (id, name, value, created_at) VALUES ('2', 'inner', 1, 'now')")
                raise ValueError("inner")
        self.assertEqual([r["name"] for r in db.fetchall("SELECT name FROM metrics")], ["outer"])

    def test_database_errors_are_wrapped(self):
        db = make_db()
        with self.assertRaises(DatabaseError):
            db.execute("SELECT * FROM no_such_table")

    def test_unsupported_url(self):
        with self.assertRaises(DatabaseError):
            Database("mysql://x")

    def test_ping_false_when_closed(self):
        db = make_db()
        db.close()
        self.assertFalse(db.ping())


if __name__ == "__main__":
    unittest.main()
