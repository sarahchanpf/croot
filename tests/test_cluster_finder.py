"""Tests for the dedicated LLM cluster builder (mocked Anthropic client)."""

from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm
from app.core import cluster_finder
from app.core.criteria import Criteria


def fake_client(companies, holder):
    block = types.SimpleNamespace(type="tool_use", name="set_cluster",
                                  input={"companies": companies})
    resp = types.SimpleNamespace(content=[block])

    def create(**kw):
        holder.update(kw)
        return resp
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


class FindCluster(unittest.TestCase):
    def setUp(self):
        self._orig = llm.client
        self.holder = {}

    def tearDown(self):
        llm.client = self._orig

    def test_returns_companies_and_excludes_hiring_company(self):
        llm.client = lambda: fake_client(["Ramp", "Mercury", "Brex", "Stripe"], self.holder)
        crit = Criteria(title="Backend Engineer", hiring_company="Brex",
                        cluster_hint="fintech peers of Brex")
        out = cluster_finder.find_cluster(crit)
        self.assertIn("Ramp", out)
        self.assertNotIn("Brex", out)          # hiring company filtered out
        self.assertEqual(len(out), len(set(o.lower() for o in out)))  # deduped

    def test_uses_cluster_model_and_forces_tool(self):
        llm.client = lambda: fake_client(["Ramp"], self.holder)
        cluster_finder.find_cluster(Criteria(cluster_hint="fintech"))
        from app import config
        self.assertEqual(self.holder["model"], config.CLUSTER_MODEL)
        self.assertEqual(self.holder["tool_choice"], {"type": "tool", "name": "set_cluster"})

    def test_no_key_returns_empty(self):
        from app import config
        key = config.ANTHROPIC_API_KEY
        config.ANTHROPIC_API_KEY = ""
        try:
            self.assertEqual(cluster_finder.find_cluster(Criteria(cluster_hint="x")), [])
        finally:
            config.ANTHROPIC_API_KEY = key

    def test_api_error_is_fail_soft(self):
        def boom():
            c = types.SimpleNamespace()
            def create(**kw):
                raise RuntimeError("upstream down")
            c.messages = types.SimpleNamespace(create=create)
            return c
        llm.client = boom
        self.assertEqual(cluster_finder.find_cluster(Criteria(cluster_hint="x")), [])


if __name__ == "__main__":
    unittest.main()
