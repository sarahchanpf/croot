"""Conversational intake — the only place Claude is used.

Given the running conversation (and any JD text already fetched), Claude returns
two things in one turn:
  1. a natural-language reply (streamed to the chat), and
  2. a structured `set_criteria` tool call whose schema IS the Criteria
     contract — so extraction is reliable, not scraped from prose.

Claude proposes the criteria card and asks for the few missing high-value
fields, but never blocks: if the user says "just search", partial criteria are
accepted (the skill is deliberately "dialed-down").
"""

from __future__ import annotations

from .criteria import Criteria

# Tool schema handed to Claude so it emits structured criteria alongside its
# chat reply. Mirrors the Criteria dataclass. Keep the two in sync.
SET_CRITERIA_TOOL = {
    "name": "set_criteria",
    "description": (
        "Record the structured search criteria extracted so far from the "
        "recruiter's messages and any job description. Always include both a "
        "floor and a ceiling for years of experience. Leave fields empty when "
        "the recruiter hasn't specified them — do not invent values."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "title_variants": {"type": "array", "items": {"type": "string"}},
            "seniority": {"type": "string"},
            "yoe_min": {"type": ["integer", "null"]},
            "yoe_max": {"type": ["integer", "null"]},
            "location": {"type": "string"},
            "location_country": {"type": "string"},
            "remote_ok": {"type": "boolean"},
            "must_have_skills": {"type": "array", "items": {"type": "string"}},
            "nice_to_have_skills": {"type": "array", "items": {"type": "string"}},
            "domain_signals": {"type": "array", "items": {"type": "string"}},
            "education": {
                "type": "object",
                "properties": {
                    "majors": {"type": "array", "items": {"type": "string"}},
                    "schools": {"type": "array", "items": {"type": "string"}},
                    "degrees": {"type": "array", "items": {"type": "string"}},
                },
            },
            "anchor_companies": {"type": "array", "items": {"type": "string"}},
            "anchor_industries": {"type": "array", "items": {"type": "string"}},
            "exclude_employers": {"type": "array", "items": {"type": "string"}},
            "title_excludes": {"type": "array", "items": {"type": "string"}},
            "hiring_company": {"type": "string"},
        },
    },
}


def run_turn(messages: list[dict], jd_text: str = "") -> tuple[str, Criteria, bool]:
    """One intake turn.

    Args:
        messages: prior conversation [{role, content}, ...].
        jd_text:  pre-fetched JD text (from a pasted URL/document), if any.
    Returns:
        (assistant_reply, criteria, ready_to_search)

    TODO(impl): call llm.client().messages with the system prompt (prompt-
    cached), the SET_CRITERIA_TOOL, the conversation, and any jd_text. Parse the
    tool call into a Criteria; derive ready_to_search from the model's signal /
    the user saying "just search".
    """
    raise NotImplementedError("intake.run_turn — see TODO")
