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

from app.core import ranker
from app.core.criteria import Criteria
from app.core.pool import compress
from app.core.ranker import plan_relaxation, rank, score_one


class _NoLLM(unittest.TestCase):
    """Force the deterministic fallback so rank() never hits the network, even
    if ANTHROPIC_API_KEY happens to be set in the dev environment."""

    def setUp(self):
        self._orig_available = ranker.llm.available
        ranker.llm.available = lambda: False

    def tearDown(self):
        ranker.llm.available = self._orig_available


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
        self.assertIn("Rust", s["rationale"])

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

    def test_no_cluster_tier_key_emitted(self):
        # The cluster-pedigree slot / tier are gone (skill parity): score_one
        # judges fit only, and never takes anchor ids.
        s = score_one(self.cand, Criteria(title="Backend Engineer", tenure_floor_months=None))
        self.assertNotIn("cluster_tier", s)
        self.assertEqual(set(s), {"score", "rationale", "flags"})

    def test_anchor_only_search_gets_neutral_score(self):
        crit = Criteria(anchor_strategy="companies", anchor_companies=["Stripe"],
                        tenure_floor_months=None)
        self.assertEqual(score_one(self.cand, crit)["score"], 70)


class RankOrdering(_NoLLM):
    def test_sorts_purely_by_fit_no_tier_override(self):
        # A high-fit candidate OUTSIDE the anchor cluster now ranks ABOVE a
        # low-fit candidate inside it — the opposite of the old tier-sort. The
        # anchor cluster is enforced by the search filter, not the ranker.
        peer = raw_profile(                               # in-cluster, weak fit
            person_id="peer", summary="", skills=[],
            current_employers=[{"name": "PayPal", "title": "Office Manager",
                                "company_id": 10, "seniority_level": "senior"}],
            past_employers=[],
        )
        strong = raw_profile(                             # out-of-cluster, strong fit
            person_id="strong", skills=["Go", "Kubernetes"],
            summary="Backend systems at fintech scale.",
            current_employers=[{"name": "Salesforce", "title": "Senior Backend Engineer",
                                "company_id": 99, "seniority_level": "senior",
                                "company_industry": "Fintech"}],
            past_employers=[],
        )
        ranked = rank(compress([peer, strong]), Criteria(
            title="Backend Engineer", must_have_skills=["Go", "Kubernetes"],
            domain_signals=["fintech"], seniority="senior",
            yoe_min=5, yoe_max=10, location="New York", tenure_floor_months=None,
        ))
        self.assertEqual(ranked[0]["person_id"], "strong")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])


class RankFiltering(_NoLLM):
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


class _Block:
    """Stand-in for an Anthropic tool_use content block."""
    def __init__(self, scores):
        self.type = "tool_use"
        self.name = "score_candidates"
        self.input = {"scores": scores}


class _Resp:
    def __init__(self, scores):
        self.content = [_Block(scores)]


class _FakeClient:
    """Records the model call and returns canned per-index scores."""
    def __init__(self, scores):
        self._scores = scores
        self.calls = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._scores)


class LLMScoring(unittest.TestCase):
    def setUp(self):
        self._orig_available = ranker.llm.available
        self._orig_client = ranker.llm.client
        ranker.llm.available = lambda: True

    def tearDown(self):
        ranker.llm.available = self._orig_available
        ranker.llm.client = self._orig_client

    def test_uses_llm_scores_and_sorts_by_them(self):
        fake = _FakeClient([
            {"index": 0, "score": 42, "rationale": "weak", "flags": []},
            {"index": 1, "score": 91, "rationale": "strong", "flags": ["star"]},
        ])
        ranker.llm.client = lambda: fake
        cands = compress([raw_profile(person_id="a"), raw_profile(person_id="b")])
        ranked = rank(cands, Criteria(title="Backend Engineer", tenure_floor_months=None))
        self.assertEqual(fake.calls[0]["model"], ranker.config.RANK_MODEL)
        self.assertEqual([c["person_id"] for c in ranked], ["b", "a"])  # sorted by LLM score
        self.assertEqual(ranked[0]["score"], 91)
        self.assertEqual(ranked[0]["rationale"], "strong")
        self.assertEqual(ranked[0]["flags"], ["star"])

    def test_missing_index_falls_back_to_deterministic(self):
        fake = _FakeClient([{"index": 0, "score": 80, "rationale": "ok", "flags": []}])
        ranker.llm.client = lambda: fake
        cands = compress([raw_profile(person_id="a"), raw_profile(person_id="b")])
        ranked = rank(cands, Criteria(must_have_skills=["Go", "Kubernetes"], tenure_floor_months=None))
        # Both scored (one by LLM, one by deterministic fallback) — none dropped.
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all(isinstance(c["score"], int) for c in ranked))

    def test_llm_failure_falls_back_to_deterministic(self):
        def boom():
            raise RuntimeError("model down")
        ranker.llm.client = boom
        cands = compress([raw_profile(person_id="a")])
        ranked = rank(cands, Criteria(must_have_skills=["Go", "Kubernetes"], tenure_floor_months=None))
        self.assertEqual(len(ranked), 1)               # deterministic fallback kept it
        self.assertGreater(ranked[0]["score"], 0)


class TitleWeighting(unittest.TestCase):
    def test_current_title_match_scores_higher_than_past_only(self):
        cur = compress([raw_profile()])[0]  # current title "Senior Backend Engineer"
        # A profile whose CURRENT role is unrelated but PAST role matched the title.
        past = raw_profile(person_id="p2",
                           current_employers=[{"name": "Acme", "title": "Product Manager", "company_id": 5}],
                           past_employers=[{"name": "Old", "title": "Backend Engineer", "company_id": 6}])
        past_c = compress([past])[0]
        crit = Criteria(title="Backend Engineer", tenure_floor_months=None)
        self.assertGreater(score_one(cur, crit)["score"], score_one(past_c, crit)["score"])


class Relaxation(unittest.TestCase):
    def test_broadens_title_before_dropping_anchor(self):
        # Under a company anchor, the title is the over-narrower — broaden it
        # (drop variants first) and KEEP the anchor. Skills are scoring-only here
        # so they're not the first relaxation.
        crit = Criteria(title="Backend Engineer", title_variants=["Y"],
                        must_have_skills=["Go"], anchor_companies=["Stripe"])
        new, radius, label = plan_relaxation(crit)
        self.assertEqual(new.title_variants, [])
        self.assertEqual(new.must_have_skills, ["Go"])        # not dropped
        self.assertEqual(new.anchor_companies, ["Stripe"])    # anchor kept
        self.assertIn("title", label)

    def test_broadens_title_to_head_noun_when_no_variants(self):
        crit = Criteria(title="Backend Engineer", anchor_companies=["Stripe"])
        new, radius, label = plan_relaxation(crit)
        self.assertEqual(new.title, "Engineer")               # head noun
        self.assertEqual(new.anchor_companies, ["Stripe"])    # anchor still kept
        self.assertIn("Engineer", label)

    def test_multi_variant_title_reduces_to_cores_not_dropped(self):
        # Regression: dropping variants NARROWS the title OR (removes alternatives)
        # and made thin pools thinner. Broadening must reduce every form to its
        # role core so every prior match still matches.
        crit = Criteria(title="Solutions Architect",
                        title_variants=["Solutions Engineer", "Sales Engineer"],
                        anchor_companies=["Stripe"])
        new, radius, label = plan_relaxation(crit)
        cores = {new.title.lower(), *(v.lower() for v in new.title_variants)}
        self.assertEqual(cores, {"architect", "engineer"})    # deduped cores, not dropped
        self.assertEqual(new.anchor_companies, ["Stripe"])    # anchor kept

    def test_drops_skills_only_in_skills_only_search(self):
        crit = Criteria(must_have_skills=["Go"])              # nothing else to search on
        new, radius, label = plan_relaxation(crit)
        self.assertEqual(new.must_have_skills, [])
        self.assertIn("skills", label)

    def test_single_word_title_does_not_head_noun(self):
        # "Engineer" has no broader form — skip title, fall through to anchor.
        crit = Criteria(title="Engineer", anchor_companies=["Stripe"])
        new, radius, label = plan_relaxation(crit)
        self.assertEqual(new.title, "Engineer")               # untouched
        self.assertEqual(new.anchor_companies, [])            # anchor dropped instead
        self.assertIn("anchor", label)

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
