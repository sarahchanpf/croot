"""Dedicated LLM cluster builder.

A separate, higher-effort Claude call (Opus) whose only job is to assemble the
best candidate-sourcing cluster — the companies whose current and former
employees are the strongest fits for a given role. Kept separate from the
intake (which runs on cheap Sonnet) so cluster quality gets a stronger model
and a focused prompt, tuned to the exact role.

Role-aware: the right "peer" depends on the FUNCTION. Engineers transfer best
within a sub-vertical/stack, so a technical role anchors on direct competitors.
A salesperson (AE/SDR/CS) transfers across sub-verticals, so a GTM role anchors
on companies with elite SALES talent at a similar stage/segment — not just the
hiring company's product competitors. Using the eng lens for a sales role was
why a Founding-AE search clustered on data-API competitors instead of the
strong-GTM startups a recruiter actually wants.

Fail-soft: returns [] on any error or when no LLM key is configured — a missing
cluster just means the search isn't company-anchored, never a crash.
"""

from __future__ import annotations

import re

from .. import config, llm
from .criteria import Criteria

# --- Role-function detection (word-boundary matched on title + seniority) ---
_GTM_TOKENS = (
    "sales", "account executive", "ae", "account manager", "account director",
    "business development", "bdr", "sdr", "customer success", "devrel",
    "developer advocate", "go-to-market", "gtm", "partnerships",
    "solutions engineer", "sales engineer", "solutions architect", "solutions consultant",
    "revenue", "growth", "field cmo", "marketing",
)
_TECH_TOKENS = (
    "engineer", "engineering", "developer", "programmer", "architect", "sre",
    "devops", "data scientist", "machine learning", "backend", "frontend",
    "full stack", "full-stack", "mobile", "ios", "android", "platform",
    "infrastructure", "scientist", "researcher", "security",
)

BASE_INTRO = (
    "You are an expert recruiter assembling a candidate-sourcing CLUSTER: the set "
    "of companies whose current and former employees are the strongest candidates "
    "for a specific role.\n\nGiven the role, the hiring/target company, and the "
    "domain, return the most relevant REAL companies."
)

_GUIDANCE = {
    "gtm": (
        "This is a GO-TO-MARKET / SALES role. The best candidates are top performers "
        "from companies with ELITE SALES/GTM ORGS at a similar stage, size, and "
        "customer segment (SMB / mid-market / enterprise) — NOT just direct product "
        "competitors. A strong seller transfers across sub-verticals, so favor "
        "well-known high-growth B2B SaaS companies famous for their go-to-market "
        "talent and a comparable sales motion, plus a few direct competitors for "
        "domain familiarity. Match the SALES MOTION and segment, not the product "
        "category."
    ),
    "technical": (
        "This is a TECHNICAL / ENGINEERING role. Engineers transfer best within a "
        "domain, so favor DIRECT COMPETITORS and close peers in the same SUB-VERTICAL, "
        "tech stack, and stage (e.g. for a payments-infra role, payments/ledger/"
        "card-issuing companies — not generic \"big tech\"), plus a few companies "
        "specifically renowned for talent in this exact area."
    ),
    "other": (
        "Favor companies at a similar stage and size in the same space — close peers "
        "and direct competitors — plus a few adjacent companies renowned for talent "
        "in this function."
    ),
}

BASE_RULES = (
    "Rules:\n"
    "- Be specific to the role and seniority. Avoid generic mega-caps unless they are "
    "genuinely the best talent source for THIS role.\n"
    "- Exclude the hiring company itself.\n"
    "- Use real, well-known company names that resolve on LinkedIn (canonical names, "
    'no suffixes like "Inc.").\n'
    "- Return 8–15 companies, most relevant first."
)

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


def _has_token(text: str, tokens) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens)


def role_function(criteria: Criteria) -> str:
    """Classify the role as 'gtm', 'technical', or 'other' from the title /
    variants / seniority. GTM is checked first so 'Solutions Architect' /
    'Sales Engineer' (pre-sales) classify as GTM, not technical."""
    text = " ".join([
        criteria.title or "", " ".join(criteria.title_variants or []),
        criteria.seniority or "",
    ]).lower()
    if _has_token(text, _GTM_TOKENS):
        return "gtm"
    if _has_token(text, _TECH_TOKENS):
        return "technical"
    return "other"


def _system_prompt(function: str) -> str:
    return f"{BASE_INTRO}\n\n{_GUIDANCE[function]}\n\n{BASE_RULES}"


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
            system=[{"type": "text", "text": _system_prompt(role_function(criteria)),
                     "cache_control": {"type": "ephemeral"}}],
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
