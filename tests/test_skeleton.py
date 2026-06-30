"""Skeleton tests — pin the implemented pieces (the Criteria contract, the CSV
destination, and that the app boots with all routes registered). Logic modules
(filters, crustdata, pool, ranker, intake) are stubbed and tested as they land.

Run with:  python -m unittest tests.test_skeleton
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.criteria import Criteria, DEFAULT_TENURE_FLOOR_MONTHS
from app.core.export.csv_dest import CSVDestination, COLUMNS


class CriteriaContract(unittest.TestCase):
    def test_round_trips_through_dict(self):
        c = Criteria(title="Backend Engineer", must_have_skills=["go", "k8s"],
                     yoe_min=5, yoe_max=10)
        c.education.schools = ["Stanford University"]
        again = Criteria.from_dict(c.to_dict())
        self.assertEqual(again.title, "Backend Engineer")
        self.assertEqual(again.must_have_skills, ["go", "k8s"])
        self.assertEqual(again.yoe_min, 5)
        self.assertEqual(again.education.schools, ["Stanford University"])

    def test_default_tenure_floor(self):
        self.assertEqual(Criteria().tenure_floor_months, DEFAULT_TENURE_FLOOR_MONTHS)

    def test_empty_criteria_is_detected(self):
        self.assertTrue(Criteria().is_empty())
        self.assertFalse(Criteria(title="PM").is_empty())

    def test_from_dict_ignores_unknown_keys(self):
        c = Criteria.from_dict({"title": "PM", "bogus": 123})
        self.assertEqual(c.title, "PM")

    def test_ai_focus_alias_is_normalized(self):
        c = Criteria.from_dict({"ai_focus": "ML engineering"})
        self.assertEqual(c.ai_focus, "model_engineering")


class CSVExport(unittest.TestCase):
    def test_writes_header_and_rows(self):
        out = CSVDestination().write(
            [{"name": "Ada", "current_company": "X", "score": 91,
              "top_skills": ["a", "b"], "personal_phone": "+1 555 0100"}],
            {"role": "Backend Engineer"},
        )
        self.assertEqual(out.kind, "csv")
        text = out.content.decode("utf-8")
        self.assertIn(",".join(COLUMNS[:2]), text)   # header present
        self.assertIn("Ada", text)
        self.assertIn("'+1 555 0100", text)          # phone apostrophe-guarded
        self.assertTrue(out.filename.endswith(".csv"))


class AppBoots(unittest.TestCase):
    def test_app_imports_and_serves_index(self):
        from app import app
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)

    def test_search_guards_empty_criteria(self):
        from app import app
        client = app.test_client()
        # /api/search now requires an authenticated user (free-search gate), so
        # sign in via the session before checking the empty-criteria guard.
        with client.session_transaction() as sess:
            sess["access_user"] = {"name": "Ada Lovelace", "email": "ada@example.com"}
        r = client.post("/api/search", json={})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
