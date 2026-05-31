"""Dedicated LLM cluster builder.

A separate, higher-effort Claude call (Opus) whose only job is to assemble the
best candidate-sourcing cluster — the companies whose current and former
employees are the strongest fits for a given role. Kept separate from the
intake (which runs on cheap Sonnet) so cluster quality gets a stronger model
and a focused prompt, tuned to the exact sub-vertical and stage rather than
generic mega-caps.

Fail-soft: returns [] on any error or when no LLM key is configured — a missing
cluster just means the search isn't company-anchored, never a crash.
"""

from __future__ import annotations

from .. import config, llm
from .criteria import Criteria

SYSTEM_PROMPT = """You are an expert technical recruiter assembling a candidate-sourcing CLUSTER: the set of companies whose current and former employees are the strongest candidates for a specific role.

Given the role, the hiring/target company, and the domain, return the most relevant REAL companies. Favor:
- direct competitors and close peers at a similar stage, size, and SUB-VERTICAL (e.g. for a payments-infra role, payments/ledger/card-issuing companies — not generic "big tech"),
- a few adjacent companies specifically renowned for talent in this exact area.

Rules:
- Be specific to the sub-vertical and seniority. Avoid generic mega-caps unless they are genuinely the best talent source for THIS role.
- Exclude the hiring company itself.
- Use real, well-known company names that resolve on LinkedIn (canonical names, no suffixes like "Inc.").
- Return 8–15 companies, most relevant first."""

SET_CLUSTER_TOOL = {
    "name": "set_cluster",
    "description": "Record the companies to source candidates from, most relevant first.",
    "input_schema": {
        "type": "object",
        "properties": {
            "companies": {"type": "array", "items": {"type": "string"},
                          "description": "8–15 real, well-known company names, most relevant first."},
        },
        "required": ["companies"],
    },
}

MAX_TOKENS = 1024


def _context(criteria: Criteria) -> str:
    loc = criteria.location or criteria.location_country or "any"
    return (
        f"Role / title: {criteria.title or 'unspecified'}\n"
        f"Seniority: {criteria.seniority or 'unspecified'}\n"
        f"Must-have skills: {', '.join(criteria.must_have_skills) or 'unspecified'}\n"
        f"Domain / industry: {', '.join(criteria.domain_signals) or 'unspecified'}\n"
        f"Hiring company (EXCLUDE from the cluster): {criteria.hiring_company or 'n/a'}\n"
        f"Location: {loc}\n"
        f"Desired cluster: {criteria.cluster_hint or 'companies whose employees would be strong candidates for this role'}"
    )


def find_cluster(criteria: Criteria) -> list[str]:
    """Return the best peer/look-alike company cluster for this search, or []."""
    if not llm.available():
        return []
    try:
        client = llm.client()
        resp = client.messages.create(
            model=config.CLUSTER_MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[SET_CLUSTER_TOOL],
            tool_choice={"type": "tool", "name": "set_cluster"},
            messages=[{"role": "user", "content": _context(criteria)}],
        )
    except Exception:
        return []  # best-effort: a failed cluster call must not break the search

    hiring = (criteria.hiring_company or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "set_cluster":
            for name in (block.input.get("companies") or []):
                n = (name or "").strip()
                if n and n.lower() not in seen and n.lower() != hiring:
                    seen.add(n.lower())
                    out.append(n)
    return out
