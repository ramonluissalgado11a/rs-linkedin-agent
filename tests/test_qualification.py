import unittest
from dataclasses import replace
from datetime import date, timedelta

from rs_agent.qualification import evaluate, qualification_stats, qualify

from .helpers import good_candidate, make_db


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def test_good_candidate_qualifies_with_full_pipeline(self):
        q = qualify(self.db, good_candidate(self.db))
        self.assertTrue(q.qualified)
        self.assertEqual([r.stage for r in q.results],
                         ["identity_verified", "employment_verified", "industry_checked", "role_checked", "deduplicated"])
        self.assertGreaterEqual(q.score, 80)  # senior + Puerto Rico + industry headline
        stages = self.db.fetchall("SELECT stage FROM qualifications WHERE profile_id = ?", (q.profile_id,))
        self.assertEqual(stages[0]["stage"], "candidate_discovered")

    def test_provenance_required(self):
        c = replace(good_candidate(self.db), source_id="")
        results, _ = evaluate(c)
        self.assertEqual(results[-1].reason_code, "NO_PROVENANCE")

    def test_reason_codes(self):
        base = good_candidate(self.db)
        cases = {
            "INVALID_NAME": replace(base, full_name="Madonna"),
            "NO_SOURCE_URL": replace(base, source_url=None, profile_url=None),
            "NO_EMPLOYMENT_EVIDENCE": replace(base, role_title=None),
            "EXCLUDED_COMPANY": replace(base, company_name="PepsiCo Puerto Rico"),
            "EXCLUDED_PERSON": replace(base, full_name="Jonathan Durán"),
            "STALE_EVIDENCE": replace(base, evidence_date=(date.today() - timedelta(days=900)).isoformat()),
            "NO_EVIDENCE_DATE": replace(base, evidence_date=None),
            "INVALID_EVIDENCE_DATE": replace(base, evidence_date="yesterday"),
            "INDUSTRY_MISMATCH": replace(base, industry="Fashion", headline="Stylist", role_title="Senior Stylist Manager",
                                         company_name="Moda Boutique"),
            "ROLE_MISMATCH": replace(base, role_title="Volunteer logistics"),
        }
        for code, cand in cases.items():
            with self.subTest(code=code):
                results, score = evaluate(cand)
                self.assertFalse(results[-1].passed)
                self.assertEqual(results[-1].reason_code, code)
                self.assertEqual(score, 0)

    def test_rejected_candidates_are_persisted_with_reason(self):
        q = qualify(self.db, replace(good_candidate(self.db), role_title=None))
        self.assertFalse(q.qualified)
        self.assertEqual(q.rejection_reason, "NO_EMPLOYMENT_EVIDENCE")
        row = self.db.fetchone("SELECT status FROM profiles WHERE id = ?", (q.profile_id,))
        self.assertEqual(row["status"], "rejected")

    def test_duplicate_by_url_and_by_name_company(self):
        first = qualify(self.db, good_candidate(self.db))
        dup_url = qualify(self.db, replace(good_candidate(self.db), full_name="Maria R. Rivera", company_name="Other"))
        self.assertFalse(dup_url.qualified)
        self.assertEqual(dup_url.rejection_reason, "DUPLICATE_PROFILE")
        self.assertEqual(dup_url.profile_id, first.profile_id)
        a = qualify(self.db, good_candidate(self.db, name="Luis Ortiz", url=None))
        b = qualify(self.db, good_candidate(self.db, name="luis ortiz", company="CARIBE LOGISTICS GROUP INC", url=None))
        self.assertTrue(a.qualified)
        self.assertEqual(b.rejection_reason, "DUPLICATE_PROFILE")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM profiles"), 2)

    def test_stats_and_kpi_not_relaxed(self):
        qualify(self.db, good_candidate(self.db, name="Ana Diaz", url=None))
        qualify(self.db, replace(good_candidate(self.db, name="Bad Person", url=None), role_title="Intern"))
        qualify(self.db, good_candidate(self.db, name="Ana Diaz", url=None))  # duplicate
        stats = qualification_stats(self.db)
        self.assertEqual(stats["profiles_checked"], 2)
        self.assertEqual(stats["profiles_qualified"], 1)
        self.assertEqual(stats["qualification_rate"], 0.5)
        self.assertEqual(stats["candidates_discovered"], 3)
        self.assertAlmostEqual(stats["duplicate_rate"], 0.3333, places=3)
        self.assertEqual(stats["kpi_target"], 0.70)
        self.assertIn("ROLE_MISMATCH", stats["rejection_reasons"])

    def test_region_priority_scoring(self):
        pr, _ = evaluate(good_candidate(self.db)), None
        es = evaluate(replace(good_candidate(self.db), region="Africa"))
        self.assertGreater(pr[1], es[1])


if __name__ == "__main__":
    unittest.main()
