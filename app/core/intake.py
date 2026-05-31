"""Conversational intake — the only place Claude is used.

Given the running conversation (and any JD text already fetched), Claude returns
two things in one turn:
  1. a short natural-language reply (shown in the chat), and
  2. a structured `set_criteria` tool call whose schema mirrors the Criteria
     contract — so extraction is reliable, not scraped from prose.

The backend is stateless: the frontend resends the whole conversation each turn,
so Claude is told to ALWAYS return the COMPLETE criteria gathered so far
(cumulative), not just this turn's delta. Claude proposes the criteria and asks
for the few missing high-value fields, but never blocks — if the recruiter says
"just search", it sets ready_to_search with whatever's known.

Needs ANTHROPIC_API_KEY. Until that's set, llm.client() raises LLMUnavailable,
which the chat route surfaces as a 503.
"""

from __future__ import annotations

from .. import config, llm
from .criteria import ANCHOR_STRATEGIES, Criteria


class IntakeError(RuntimeError):
    """A clean, user-facing failure from the Claude call (e.g. billing / rate
    limit), so the chat route returns a readable message instead of a 500."""
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status

SYSTEM_PROMPT = """You are the intake assistant for Croot, a tool recruiters use to source candidates.

Your job each turn: read everything the recruiter has said (and any job description they provided) and capture structured SEARCH CRITERIA, then ask for the single most valuable missing detail.

Rules:
- ALWAYS call the `set_criteria` tool, every turn, with the COMPLETE criteria gathered so far across the whole conversation — not just the latest message. Fields you don't know yet stay empty. NEVER invent a value the recruiter didn't state or clearly imply.
- Years of experience always carries BOTH a floor and a ceiling. If the recruiter says "5+ years", propose a sensible ceiling for the seniority (e.g. a senior IC "5+" → 5–10) and mention they can change it. Never leave an open-ended "N+".
- Split skills into must-have vs nice-to-have based on the recruiter's language ("required"/"must" → must-have; "preferred"/"bonus"/"nice" → nice-to-have). When unclear, lean nice-to-have.
- Choose an anchor_strategy: "companies" when the brief names a clear set of target employers, "industries" when a domain matters but no specific companies, "both", or "none".
- Set hiring_company to the company doing the hiring (so its own employees are excluded), when known.
- Ask for AT MOST one or two of the highest-value MISSING fields per turn (usually: title, location, must-have skills, or YoE). Don't re-ask anything already answered. Keep your reply to 1–3 friendly sentences.
- Set ready_to_search to true when you have enough for a useful search (at least a title, or skills, or an anchor) and you've shown the recruiter the criteria — OR whenever they say to just search / that's enough. The search can run on partial criteria; never block on missing info."""

# Tool schema handed to Claude so it emits structured criteria alongside its
# chat reply. Mirrors the Criteria dataclass (+ ready_to_search control signal).
SET_CRITERIA_TOOL = {
    "name": "set_criteria",
    "description": (
        "Record the COMPLETE structured search criteria gathered so far from the "
        "recruiter's messages and any job description. Call this every turn with "
        "the full cumulative criteria. Leave fields empty when not specified — "
        "do not invent values. Always include both a floor and a ceiling for "
        "years of experience."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Primary role title."},
            "title_variants": {"type": "array", "items": {"type": "string"},
                                "description": "Synonyms/abbreviations for the title."},
            "seniority": {"type": "string"},
            "yoe_min": {"type": ["integer", "null"]},
            "yoe_max": {"type": ["integer", "null"]},
            "location": {"type": "string", "description": "City or metro. Leave empty if remote."},
            "location_country": {"type": "string", "description": "Use for country-wide searches."},
            "remote_ok": {"type": "boolean"},
            "must_have_skills": {"type": "array", "items": {"type": "string"}},
            "nice_to_have_skills": {"type": "array", "items": {"type": "string"}},
            "domain_signals": {"type": "array", "items": {"type": "string"},
                                "description": "Industries / sub-specialties, e.g. 'fintech'."},
            "career_path_signals": {"type": "array", "items": {"type": "string"}},
            "education": {
                "type": "object",
                "properties": {
                    "majors": {"type": "array", "items": {"type": "string"}},
                    "schools": {"type": "array", "items": {"type": "string"}},
                    "degrees": {"type": "array", "items": {"type": "string"}},
                },
            },
            "anchor_strategy": {"type": "string", "enum": list(ANCHOR_STRATEGIES)},
            "anchor_companies": {"type": "array", "items": {"type": "string"},
                                  "description": "Target employers candidates should have worked at."},
            "anchor_industries": {"type": "array", "items": {"type": "string"}},
            "exclude_employers": {"type": "array", "items": {"type": "string"}},
            "title_excludes": {"type": "array", "items": {"type": "string"}},
            "tenure_floor_months": {"type": ["integer", "null"],
                                     "description": "Min months at current employer. null allows recent joiners."},
            "hiring_company": {"type": "string"},
            "ready_to_search": {"type": "boolean",
                                 "description": "True when there's enough to search, or the recruiter said to search now."},
        },
    },
}

MAX_TOKENS = 1024
_JD_CONTEXT_MAX_CHARS = 20000


def _to_api_messages(messages: list[dict], jd_text: str) -> list[dict]:
    """Normalise the frontend conversation into Anthropic message shape:
    alternating-ish user/assistant, starting with a user turn. Any JD text is
    appended to the latest user message (Anthropic rejects consecutive user
    messages, so we merge rather than append a new one)."""
    api: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            api.append({"role": role, "content": content})
    # Must start with a user turn.
    while api and api[0]["role"] == "assistant":
        api.pop(0)
    if jd_text.strip():
        note = f"\n\n[Job description provided]\n{jd_text[:_JD_CONTEXT_MAX_CHARS]}"
        if api and api[-1]["role"] == "user":
            api[-1]["content"] += note
        else:
            api.append({"role": "user", "content": note.strip()})
    return api


def run_turn(messages: list[dict], jd_text: str = "") -> tuple[str, Criteria, bool]:
    """One intake turn. Returns (assistant_reply, criteria, ready_to_search).

    Raises LLMUnavailable (caught by the chat route as 503) when there's no key.
    """
    api_messages = _to_api_messages(messages, jd_text)
    if not api_messages:
        return ("Tell me who you're looking for — a role, a few requirements, "
                "or paste a job description.", Criteria(), False)

    client = llm.client()
    try:
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[SET_CRITERIA_TOOL],
            messages=api_messages,
        )
    except Exception as exc:  # anthropic.APIError and friends — surface cleanly
        body = getattr(exc, "body", None)
        msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        raise IntakeError(msg or getattr(exc, "message", None) or str(exc))

    reply_parts: list[str] = []
    tool_input: dict | None = None
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            reply_parts.append(block.text)
        elif btype == "tool_use" and getattr(block, "name", None) == "set_criteria":
            tool_input = dict(block.input or {})

    ready = False
    crit_dict: dict = {}
    if tool_input is not None:
        ready = bool(tool_input.pop("ready_to_search", False))
        crit_dict = tool_input
    criteria = Criteria.from_dict(crit_dict)

    reply = "\n".join(p for p in reply_parts if p).strip()
    if not reply:
        reply = "Got it — I've updated the criteria on the right."
    return reply, criteria, ready
