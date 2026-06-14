"""Tests for the per-user saved-searches routes. The webhook layer
(notify.get_webhook / request_webhook) is monkeypatched, so no network.

Run with:  python -m unittest tests.test_saved_searches
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg
from app.routes import history as hist


class SavedSearches(unittest.TestCase):
    def setUp(self):
        self.client = app_pkg.app.test_client()
        self._get, self._req = hist.get_webhook, hist.request_webhook

    def tearDown(self):
        hist.get_webhook, hist.request_webhook = self._get, self._req

    def _signin(self, email="ada@example.com"):
        with self.client.session_transaction() as s:
            s["access_user"] = {"name": "Ada", "email": email}

    def test_requires_signin(self):
        self.assertEqual(self.client.get("/api/saved-searches").status_code, 401)
        self.assertEqual(
            self.client.post("/api/saved-searches", json={"name": "x", "criteria": {}}).status_code, 401)
        self.assertEqual(self.client.delete("/api/saved-searches/abc").status_code, 401)

    def test_list_parses_criteria_json(self):
        self._signin()
        hist.get_webhook = lambda params: [
            {"id": "1", "name": "NYC BE", "query": "Backend Engineer",
             "criteria": '{"title": "Backend Engineer"}'},
        ]
        r = self.client.get("/api/saved-searches")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()[0]["criteria"], {"title": "Backend Engineer"})

    def test_save_proxies_scoped_to_user(self):
        self._signin()
        captured = {}

        def fake_req(payload):
            captured.update(payload)
            return {"ok": True, "id": "abc"}

        hist.request_webhook = fake_req
        r = self.client.post("/api/saved-searches",
                             json={"name": "My search", "criteria": {"title": "X"}})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["id"], "abc")
        self.assertEqual(captured["event"], "save_search")
        self.assertEqual(captured["email"], "ada@example.com")

    def test_save_validates_name_and_criteria(self):
        self._signin()
        self.assertEqual(
            self.client.post("/api/saved-searches", json={"name": "", "criteria": {}}).status_code, 400)

    def test_delete_proxies(self):
        self._signin()
        hist.request_webhook = lambda payload: {"ok": True}
        r = self.client.delete("/api/saved-searches/abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["deleted"], "abc")

    def test_storage_unavailable_is_502(self):
        self._signin()
        hist.request_webhook = lambda payload: None
        r = self.client.post("/api/saved-searches", json={"name": "x", "criteria": {}})
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
