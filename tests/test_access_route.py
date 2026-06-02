"""Tests for the alpha access gate."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg
import app.routes.access as access_route


class AccessGate(unittest.TestCase):
    def setUp(self):
        self.client = app_pkg.app.test_client()
        self._orig_db = access_route.db
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()

        conn = sqlite3.connect(self.tmp.name)
        conn.execute(
            """
            CREATE TABLE access_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                user_agent TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        def test_db():
            conn = sqlite3.connect(self.tmp.name)
            conn.row_factory = sqlite3.Row
            return conn

        access_route.db = test_db

    def tearDown(self):
        access_route.db = self._orig_db
        os.unlink(self.tmp.name)

    def test_rejects_wrong_password(self):
        r = self.client.post("/api/access", json={"password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_password_only_advances_to_profile_step(self):
        r = self.client.post("/api/access", json={"password": "crootalphaV1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["next"], "profile")

    def test_stores_name_and_email(self):
        r = self.client.post("/api/access", json={
            "password": "crootalphaV1",
            "name": "Ada Lovelace",
            "email": "ada@example.com",
        })
        self.assertEqual(r.status_code, 200)

        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute("SELECT name, email FROM access_users").fetchone()
        conn.close()
        self.assertEqual(row, ("Ada Lovelace", "ada@example.com"))


if __name__ == "__main__":
    unittest.main()
