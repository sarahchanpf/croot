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

    def test_role_function_classification(self):
        rf = cluster_finder.role_function
        self.assertEqual(rf(Criteria(title="Founding Account Executive")), "gtm")
        self.assertEqual(rf(Criteria(title="Solutions Architect")), "gtm")   # pre-sales
        self.assertEqual(rf(Criteria(title="Senior Backend Engineer")), "technical")
        self.assertEqual(rf(Criteria(title="Data Scientist")), "technical")
        self.assertEqual(rf(Criteria(title="Product Manager")), "other")

    def test_gtm_role_gets_sales_talent_prompt(self):
        llm.client = lambda: fake_client(["Ramp"], self.holder)
        cluster_finder.find_cluster(Criteria(title="Account Executive"))
        sys_text = self.holder["system"][0]["text"]
        self.assertIn("GO-TO-MARKET", sys_text)
        self.assertNotIn("TECHNICAL / ENGINEERING", sys_text)

    def test_technical_role_gets_subvertical_prompt(self):
        llm.client = lambda: fake_client(["Stripe"], self.holder)
        cluster_finder.find_cluster(Criteria(title="Backend Engineer"))
        sys_text = self.holder["system"][0]["text"]
        self.assertIn("SUB-VERTICAL", sys_text)
        self.assertNotIn("GO-TO-MARKET", sys_text)

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
