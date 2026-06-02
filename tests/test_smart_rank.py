"""Tests for the smart-rank Opus pass (mocked Anthropic client)."""

from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm
from app.core import smart_rank
from app.core.criteria import Criteria


def fake_client(rankings, holder):
    block = types.SimpleNamespace(type="tool_use", name="set_rankings",
                                  input={"rankings": rankings})
    resp = types.SimpleNamespace(content=[block])

    def create(**kw):
        holder.update(kw)
        return resp
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def cands():
    # deterministic order: a(90) then b(50)
    return [
        {"person_id": "a", "name": "A", "score": 90, "crustdata_rank": 0},
        {"person_id": "b", "name": "B", "score": 50, "crustdata_rank": 1},
    ]


class SmartRank(unittest.TestCase):
    def setUp(self):
        self._orig = llm.client
        self.holder = {}

    def tearDown(self):
        llm.client = self._orig

    def test_rescores_and_reorders(self):
        # Opus flips the order: b is the better fit.
        llm.client = lambda: fake_client(
            [{"person_id": "a", "fit_score": 40, "rationale": "weak"},
             {"person_id": "b", "fit_score": 95, "rationale": "strong"}], self.holder)
        out = smart_rank.rank(cands(), Criteria(title="Engineer"))
        self.assertEqual([c["person_id"] for c in out], ["b", "a"])
        self.assertEqual(out[0]["score"], 95)
        self.assertTrue(out[0]["smart_ranked"])
        self.assertEqual(out[0]["rationale"], "strong")

    def test_uses_rank_model_and_forces_tool(self):
        llm.client = lambda: fake_client([{"person_id": "a", "fit_score": 80}], self.holder)
        smart_rank.rank(cands(), Criteria(title="Engineer"))
        from app import config
        self.assertEqual(self.holder["model"], config.RANK_MODEL)
        self.assertEqual(self.holder["tool_choice"], {"type": "tool", "name": "set_rankings"})

    def test_no_key_returns_unchanged(self):
        from app import config
        key = config.ANTHROPIC_API_KEY
        config.ANTHROPIC_API_KEY = ""
        try:
            original = cands()
            self.assertEqual(smart_rank.rank(original, Criteria()), original)
        finally:
            config.ANTHROPIC_API_KEY = key

    def test_api_error_is_fail_soft(self):
        def boom():
            def create(**kw):
                raise RuntimeError("down")
            return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        llm.client = boom
        original = cands()
        self.assertEqual(smart_rank.rank(original, Criteria()), original)


if __name__ == "__main__":
    unittest.main()
