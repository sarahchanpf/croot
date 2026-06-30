"""Tests for opt-in profile enrichment."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg
from app.core import crustdata


class ProfileRoute(unittest.TestCase):
    def setUp(self):
        self.client = app_pkg.app.test_client()
        self._orig_enrich = crustdata.enrich

    def tearDown(self):
        crustdata.enrich = self._orig_enrich

    def test_full_profile_normalizes_external_sources(self):
        calls = []

        def fake_enrich(urls, include_contact=True, include_full=False):
            calls.append((urls, include_contact, include_full))
            return {"profiles": [{
                "name": "Ada Lovelace",
                "linkedin_profile_url": urls[0],
                "github_profile_url": "https://github.com/ada",
                "google_scholar_url": "https://scholar.google.com/citations?user=ada",
                "publications": [{"title": "Notes on Analytical Engines", "year": 1843}],
                "github_repositories": [{"name": "engine", "url": "https://github.com/ada/engine"}],
            }]}

        crustdata.enrich = fake_enrich
        r = self.client.get("/api/profile?full=true&linkedin_url=https://linkedin.com/in/ada")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[0], (["https://linkedin.com/in/ada"], True, True))
        full = r.get_json()["full_profile"]
        self.assertEqual(full["github_url"], "https://github.com/ada")
        self.assertIn("scholar.google.com", full["scholar_url"])
        self.assertEqual(full["scholar_articles"][0]["title"], "Notes on Analytical Engines")
        self.assertEqual(full["github_repositories"][0]["name"], "engine")


if __name__ == "__main__":
    unittest.main()
