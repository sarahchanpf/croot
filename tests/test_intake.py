"""Tests for the Claude intake turn.

The Anthropic client is mocked, so the message-shaping and tool-call parsing are
verified with no ANTHROPIC_API_KEY. The unmocked path is asserted to raise
LLMUnavailable (which the chat route turns into a 503).

Run with:  python -m unittest tests.test_intake
"""

from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg
from app import llm
from app.core import intake
from app.llm import LLMUnavailable


def fake_response(text, tool_input):
    blocks = []
    if text is not None:
        blocks.append(types.SimpleNamespace(type="text", text=text))
    if tool_input is not None:
        blocks.append(types.SimpleNamespace(type="tool_use", name="set_criteria", input=tool_input))
    return types.SimpleNamespace(content=blocks)


def fake_client(resp, holder):
    def create(**kw):
        holder.update(kw)
        return resp
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


class MessageShaping(unittest.TestCase):
    def test_strips_leading_assistant_and_starts_with_user(self):
        api = intake._to_api_messages(
            [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "hello"}], "")
        self.assertEqual(api[0]["role"], "user")

    def test_jd_text_merged_into_last_user_message(self):
        api = intake._to_api_messages([{"role": "user", "content": "find me PMs"}], "JD BODY HERE")
        self.assertEqual(len(api), 1)               # not a second user message
        self.assertIn("find me PMs", api[0]["content"])
        self.assertIn("JD BODY HERE", api[0]["content"])

    def test_empty_conversation_returns_greeting_without_calling_model(self):
        reply, crit, ready = intake.run_turn([])
        self.assertIn("looking for", reply.lower())
        self.assertTrue(crit.is_empty())
        self.assertFalse(ready)


class ToolParsing(unittest.TestCase):
    def setUp(self):
        self._orig = llm.client
        self.holder = {}

    def tearDown(self):
        llm.client = self._orig

    def test_parses_criteria_and_ready_flag(self):
        resp = fake_response(
            "Got a senior backend role in NYC. Any must-have skills?",
            {"title": "Backend Engineer", "seniority": "senior", "location": "New York",
             "yoe_min": 5, "yoe_max": 10, "must_have_skills": ["Go"], "ready_to_search": False},
        )
        llm.client = lambda: fake_client(resp, self.holder)

        reply, crit, ready = intake.run_turn([{"role": "user", "content": "senior backend eng in NYC, 5+ yrs"}])
        self.assertIn("must-have", reply)
        self.assertEqual(crit.title, "Backend Engineer")
        self.assertEqual(crit.yoe_max, 10)
        self.assertEqual(crit.must_have_skills, ["Go"])
        self.assertFalse(ready)
        # ready_to_search must NOT leak onto the criteria.
        self.assertNotIn("ready_to_search", crit.to_dict())

    def test_ready_to_search_true_is_lifted_off_criteria(self):
        resp = fake_response("Searching now.", {"title": "PM", "ready_to_search": True})
        llm.client = lambda: fake_client(resp, self.holder)
        _, crit, ready = intake.run_turn([{"role": "user", "content": "just search"}])
        self.assertTrue(ready)
        self.assertEqual(crit.title, "PM")

    def test_missing_text_block_gets_fallback_reply(self):
        resp = fake_response(None, {"title": "PM"})
        llm.client = lambda: fake_client(resp, self.holder)
        reply, _, _ = intake.run_turn([{"role": "user", "content": "PM"}])
        self.assertTrue(reply)                       # never empty

    def test_tools_and_system_passed_to_model(self):
        resp = fake_response("ok", {"title": "PM"})
        llm.client = lambda: fake_client(resp, self.holder)
        intake.run_turn([{"role": "user", "content": "PM"}])
        self.assertEqual(self.holder["tools"][0]["name"], "set_criteria")
        self.assertEqual(self.holder["system"][0]["cache_control"], {"type": "ephemeral"})


class NoKey(unittest.TestCase):
    """Force the no-key path regardless of whether .env has a key set."""
    def setUp(self):
        from app import config
        self._key = config.ANTHROPIC_API_KEY
        config.ANTHROPIC_API_KEY = ""

    def tearDown(self):
        from app import config
        config.ANTHROPIC_API_KEY = self._key

    def test_run_turn_raises_when_no_api_key(self):
        with self.assertRaises(LLMUnavailable):
            intake.run_turn([{"role": "user", "content": "find me engineers"}])

    def test_chat_route_returns_503_without_key(self):
        client = app_pkg.app.test_client()
        r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
