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
from .regions import REGION_COUNTRIES


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
- TITLE: set `title` to the ROLE ONLY — do NOT bake seniority words into it. "senior backend engineer" → title "Backend Engineer", seniority "Senior" (not title "Senior Backend Engineer"). Put 2–4 close equivalents in `title_variants` (e.g. Backend Engineer → "Backend Developer", "Back-End Engineer", "Backend Software Engineer") — close synonyms, not generic broadenings like plain "Software Engineer".
- LOCATION — this is WHERE THE CANDIDATE must be, NOT where the hiring company is based. A company (US or otherwise) hiring globally or remotely imposes NO candidate-location requirement: NEVER set `location` or `location_country` from the company's HQ/country. Non-US locations are first-class — handle them exactly like US ones. Apply in order:
  * A specific place is required → set `location` to a GEOCODABLE "City, Region-or-Country" string using the FULL region/country name. US: "Chicago, IL", "Austin, TX". Non-US: "London, United Kingdom", "Bengaluru, India", "Toronto, Canada", "Berlin, Germany", "São Paulo, Brazil". NEVER a bare city, a lone state, or a lone country in `location` (a bare/ambiguous city geocodes unreliably and returns people worldwide).
  * A whole-country search ("anywhere in India", "UK-based only") → leave `location` empty and set `location_country` to the FULL country name ("India", "United Kingdom", "United States").
  * A MULTI-COUNTRY region ("Europe", "APAC", "LATAM", "the Nordics", "DACH", "Middle East", "SE Asia") → leave `location` and `location_country` empty and set `location_region` to the closest key (europe, nordics, dach, benelux, uk_ireland, apac, southeast_asia, south_asia, latam, north_america, middle_east, mena, africa, oceania).
  * Remote / global / "open to candidates anywhere" / location-agnostic → leave `location`, `location_country`, and `location_region` empty and set `remote_ok` true. Do NOT infer a region or country from the company.
  * Hybrid or onsite → location-bound: set `location`, keep `remote_ok` false.
  Set at most ONE of location / location_country / location_region; never set a location AND remote_ok together; never invent a location the brief doesn't state.
- Years of experience always carries BOTH a floor and a ceiling. If the recruiter says "5+ years", propose a sensible ceiling for the seniority (e.g. a senior IC "5+" → 5–10) and mention they can change it. Never leave an open-ended "N+".
- Split skills into must-have vs nice-to-have based on the recruiter's language ("required"/"must" → must-have; "preferred"/"bonus"/"nice" → nice-to-have). When unclear, lean nice-to-have.
- Anchor strategy decides how Croot narrows to a relevant talent pool. PREFER a concrete COMPANY cluster over a bare industry whenever the brief implies a recognizable set of employers:
  * If the recruiter explicitly NAMES specific companies, put those exact companies in `anchor_companies` (and set anchor_strategy "companies"). Don't second-guess an explicit list.
  * Otherwise, when a company cluster is the right anchor — a CATEGORY of companies ("top fintech companies", "big banks", "FAANG", "elite quant firms"), OR a role AT a specific company where you should source from its LOOK-ALIKES ("backend engineer at Brex") — DO NOT hand-list the companies yourself. Instead set `cluster_hint` to a short, specific description of the cluster to build, set anchor_strategy "companies", and leave anchor_companies empty. A dedicated step then assembles the best peer set. (e.g. "top fintech companies" → cluster_hint "top fintech companies"; "backend engineer at Brex" → hiring_company "Brex", cluster_hint "close fintech peers and competitors of Brex, similar stage/size"; "quant dev" → cluster_hint "elite quantitative trading firms".)
  * When sourcing look-alikes for a hiring company, always also set `hiring_company` so its own staff are excluded.
  * Use "industries" only when a domain genuinely matters but no company cluster fits; "both" when both apply; "none" when neither. Note: a bare industry word like "fintech" does NOT map to a canonical industry value, so a company cluster is almost always the better anchor.
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
            "location": {"type": "string", "description": "Geocodable 'City, Region/Country' string with FULL region/country name — US: 'Chicago, IL'; non-US: 'London, United Kingdom', 'Bengaluru, India'. NEVER a bare city, lone state, or lone country. About the CANDIDATE's location, never the company's. Empty if fully remote, country-wide, or a multi-country region."},
            "location_country": {"type": "string", "description": "Single FULL country name (e.g. 'United States', 'India') for a whole-country search. Empty when location (city) or location_region is set."},
            "location_region": {"type": "string", "enum": list(REGION_COUNTRIES.keys()),
                                  "description": "Multi-country region for searches like 'engineers in Europe' / 'APAC' / 'LATAM'. Pick the closest key. Leave empty when a specific city (location) or single country (location_country) is given."},
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
            "cluster_hint": {
                "type": "string",
                "description": "Short description of a company cluster to build (a category, or look-alikes of the hiring company). A dedicated step turns this into the company list. Use this instead of hand-listing unless the recruiter named specific companies.",
            },
            "anchor_companies": {"type": "array", "items": {"type": "string"},
                                  "description": "Only the SPECIFIC companies the recruiter explicitly named. For a category or look-alikes, leave empty and set cluster_hint."},
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
        reply = "Got it — I've updated the criteria. Add more in Extra terms or press Execute Search to continue."
    return reply, criteria, ready
