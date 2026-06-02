"""Orchestration tests for /api/search and /api/preview.

The network layer (crustdata.search / identify / autocomplete) is monkeypatched,
so the full resolve -> build -> search -> compress -> rank -> relax -> cache
pipeline is exercised with no API key.

Run with:  python -m unittest tests.test_search_route
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg
from app.core import crustdata
from app.core.crustdata import CrustdataError


def profile(pid, title="Backend Engineer", company="Acme", cid=1, skills=("Go",)):
    return {
        "person_id": pid, "name": f"Person {pid}",
        "linkedin_url": f"https://linkedin.com/in/{pid}",
        "region": "New York, United States", "years_of_experience_raw": 6,
        "skills": list(skills),
        "current_employers": [{"name": company, "title": title, "company_id": cid,
                               "seniority_level": "senior"}],
        "past_employers": [],
    }


class SearchRouteBase(unittest.TestCase):
    def setUp(self):
        self.client = app_pkg.app.test_client()
        # Default fakes — identify resolves to a stable id, autocomplete no-ops.
        self._orig = {
            "search": crustdata.search,
            "identify": crustdata.identify,
            "autocomplete": crustdata.autocomplete,
        }
        crustdata.identify = lambda name: 999
        crustdata.autocomplete = lambda field, query: []
        # Disable cache so each test is isolated.
        import app.routes.search as sr
        self._sr = sr
        self._orig_get = sr.get_cached
        self._orig_put = sr.put_cached
        sr.get_cached = lambda key: None
        sr.put_cached = lambda *a, **k: None
        # Neutralize the smart-rank Opus pass so route tests stay offline/fast.
        self._orig_smart = sr.smart_rank.rank
        sr.smart_rank.rank = lambda ranked, criteria, top_n=None: ranked

    def tearDown(self):
        crustdata.search = self._orig["search"]
        crustdata.identify = self._orig["identify"]
        crustdata.autocomplete = self._orig["autocomplete"]
        self._sr.get_cached = self._orig_get
        self._sr.put_cached = self._orig_put
        self._sr.smart_rank.rank = self._orig_smart


class Preview(SearchRouteBase):
    def test_returns_total_count(self):
        crustdata.search = lambda payload, limit=1, sorts=None: {"total_count": 42, "profiles": []}
        r = self.client.post("/api/preview", json={"title": "Backend Engineer"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["total_count"], 42)

    def test_empty_criteria_rejected(self):
        self.assertEqual(self.client.post("/api/preview", json={}).status_code, 400)


class Search(SearchRouteBase):
    def test_returns_ranked_candidates(self):
        crustdata.search = lambda payload, limit=100, sorts=None: {
            "total_count": 50,
            "profiles": [profile("a", skills=["Go", "Kubernetes"]),
                         profile("b", skills=["COBOL"])],
        }
        r = self.client.post("/api/search", json={
            "title": "Backend Engineer", "must_have_skills": ["Go", "Kubernetes"],
        })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["returned"], 2)
        # 'a' holds both must-haves -> ranks first.
        self.assertEqual(body["candidates"][0]["person_id"], "a")
        self.assertGreater(body["candidates"][0]["score"], body["candidates"][1]["score"])
        self.assertEqual(body["relaxed"], [])

    def test_thin_pool_triggers_one_relaxation(self):
        calls = {"n": 0}

        def fake_search(payload, limit=100, sorts=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"total_count": 2, "profiles": [profile("a")]}     # thin -> relax
            return {"total_count": 30, "profiles": [profile("a"), profile("b")]}

        crustdata.search = fake_search
        r = self.client.post("/api/search", json={
            "title": "Backend Engineer", "must_have_skills": ["Go"],
        })
        body = r.get_json()
        self.assertEqual(calls["n"], 2)                       # searched twice
        self.assertTrue(body["relaxed"])                      # surfaced what was loosened
        self.assertIn("skills", body["relaxed"][0])

    def test_dedups_hiring_company(self):
        crustdata.identify = lambda name: 7 if name == "HireCo" else 999
        crustdata.search = lambda payload, limit=100, sorts=None: {
            "total_count": 20, "profiles": [profile("a", cid=7), profile("b", cid=1)],
        }
        r = self.client.post("/api/search", json={
            "title": "Backend Engineer", "hiring_company": "HireCo",
        })
        ids = [c["person_id"] for c in r.get_json()["candidates"]]
        self.assertEqual(ids, ["b"])                          # 'a' is at the hiring company

    def test_upstream_error_propagates_status(self):
        def boom(payload, limit=100, sorts=None):
            raise CrustdataError("Crustdata returned 429", 429)
        crustdata.search = boom
        r = self.client.post("/api/search", json={"title": "X"})
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
