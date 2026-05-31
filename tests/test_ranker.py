"""Tests for pool.compress and the 0-100 ranker.

Both are pure functions — no API key. A raw-ish Crustdata profile fixture is
compressed, then scored against various criteria.

Run with:  python -m unittest tests.test_ranker
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.criteria import Criteria
from app.core.pool import compress
from app.core.ranker import plan_relaxation, rank, score_one


def raw_profile(**over):
    p = {
        "person_id": "p1",
        "name": "Ada Lovelace",
        "linkedin_url": "https://linkedin.com/in/ada",
        "headline": "Backend engineer",
        "region": "New York, United States",
        "years_of_experience_raw": 7,
        "skills": ["Go", "Kubernetes", "PostgreSQL"],
        "summary": "Backend systems at fintech scale.",
        "education_background": [{"institute_name": "MIT", "field_of_study": "CS"}],
        "current_employers": [{
            "name": "Stripe", "title": "Senior Backend Engineer", "company_id": 10,
            "seniority_level": "senior", "company_industry": "Fintech",
            "start_date": "2021-01",
        }],
        "past_employers": [{"name": "Plaid", "title": "Backend Engineer", "company_id": 11,
                            "company_industry": "Fintech"}],
    }
    p.update(over)
    return p


class Compression(unittest.TestCase):
    def test_projects_expected_fields(self):
        c = compress([raw_profile()])[0]
        self.assertEqual(c["name"], "Ada Lovelace")
        self.assertEqual(c["current_company"], "Stripe")
        self.assertEqual(c["current_company_id"], 10)
        self.assertEqual(c["current_title"], "Senior Backend Engineer")
        self.assertEqual(c["yoe"], 7)
        self.assertEqual(c["prior_employers"], ["Plaid"])
        self.assertIn("Fintech", c["industries"])
        self.assertEqual(c["crustdata_rank"], 0)
        self.assertFalse(c["data_gap"])

    def test_missing_identity_flags_data_gap(self):
        c = compress([{"name": "No Links"}])[0]
        self.assertTrue(c["data_gap"])


class Scoring(unittest.TestCase):
    def setUp(self):
        self.cand = compress([raw_profile()])[0]

    def test_perfect_match_scores_100(self):
        crit = Criteria(title="Backend Engineer", must_have_skills=["Go", "Kubernetes"],
                        domain_signals=["fintech"], yoe_min=5, yoe_max=10,
                        location="New York", seniority="senior", tenure_floor_months=None)
        self.assertEqual(score_one(self.cand, crit)["score"], 100)

    def test_partial_skills_lowers_score(self):
        crit = Criteria(must_have_skills=["Go", "Rust", "Elixir"])  # 1 of 3
        s = score_one(self.cand, crit)
        self.assertEqual(s["score"], 33)
        self.assertTrue(any("Rust" in m for m in s["missed"]))

    def test_nice_to_have_never_drags(self):
        base = Criteria(title="Backend Engineer")
        with_nice = Criteria(title="Backend Engineer", nice_to_have_skills=["Haskell"])  # not held
        self.assertGreaterEqual(score_one(self.cand, with_nice)["score"],
                                score_one(self.cand, base)["score"])

    def test_nice_to_have_hit_lifts_score(self):
        base = Criteria(must_have_skills=["Go", "Rust"])              # 50%
        lifted = Criteria(must_have_skills=["Go", "Rust"], nice_to_have_skills=["PostgreSQL"])
        self.assertGreater(score_one(self.cand, lifted)["score"],
                           score_one(self.cand, base)["score"])

    def test_data_gap_caps_score(self):
        gap = compress([{"name": "X", "skills": ["Go"]}])[0]
        crit = Criteria(must_have_skills=["Go"])  # would be 100 without the cap
        self.assertLessEqual(score_one(gap, crit)["score"], 70)

    def test_anchor_only_search_gets_neutral_score(self):
        crit = Criteria(anchor_strategy="companies", anchor_companies=["Stripe"],
                        tenure_floor_months=None)
        self.assertEqual(score_one(self.cand, crit)["score"], 70)


class RankFiltering(unittest.TestCase):
    def test_drops_same_employer_as_hiring_company(self):
        cands = compress([raw_profile(), raw_profile(person_id="p2")])
        ranked = rank(cands, Criteria(title="Backend Engineer"), hiring_company_id=10)
        self.assertEqual(ranked, [])   # both currently at company 10

    def test_drops_title_excludes(self):
        cands = compress([raw_profile()])
        crit = Criteria(title="Engineer", title_excludes=["senior"])
        self.assertEqual(rank(cands, crit), [])

    def test_sorts_by_score_then_rank(self):
        strong = raw_profile(person_id="p1")
        weak = raw_profile(person_id="p2", skills=["COBOL"])
        ranked = rank(compress([weak, strong]),
                      Criteria(must_have_skills=["Go", "Kubernetes"]))
        self.assertEqual(ranked[0]["person_id"], "p1")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])


class Relaxation(unittest.TestCase):
    def test_drops_skills_first(self):
        crit = Criteria(title="X", title_variants=["Y"], must_have_skills=["Go"])
        new, radius, label = plan_relaxation(crit)
        self.assertEqual(new.must_have_skills, [])
        self.assertEqual(new.title_variants, ["Y"])   # title untouched at this step
        self.assertIn("skills", label)

    def test_widens_geo_when_only_location_left(self):
        crit = Criteria(location="New York")
        new, radius, label = plan_relaxation(crit)
        self.assertEqual(radius, 100)
        self.assertIn("radius", label)

    def test_returns_none_when_nothing_to_relax(self):
        new, radius, label = plan_relaxation(Criteria(remote_ok=True))
        self.assertIsNone(new)
        self.assertIsNone(label)


if __name__ == "__main__":
    unittest.main()
