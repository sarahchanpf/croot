"""Smart-rank — optional Opus judgment pass over the pool (emulates the skill's
Phase 6 ranking).

The deterministic rubric in ranker.py is fast and predictable but literal.
This re-scores the top N candidates (from the deterministic order) with a single
Opus reasoning call, the way the skill does — weighing title/skills/domain/YoE/
location holistically, penalizing hard misses, and explaining each score. It
re-scores only the top N to cap cost, and is fail-soft: any error (or no key)
leaves the deterministic order untouched.
"""

from __future__ import annotations

from .. import config, llm
from .criteria import Criteria

SYSTEM_PROMPT = """You are an expert technical recruiter scoring candidates against a role. For each candidate, output a 0–100 fit score for how well they match the criteria — judgment, not keyword counting.

Weight roughly: title/role match ~25, must-have skills overlap ~25, domain/industry fit ~20, YoE & seniority band ~15, location ~10, bonus signals (relevant transitions, education, certs) ~5. Bend the weights when one signal clearly dominates.

Penalize hard misses: a candidate who contradicts an exclude, is in the wrong sub-specialty, or wrong geo for an on-site role caps around 40 regardless of other signals. A candidate currently at a target/peer company is a strong positive. Thin/incomplete profiles cap around 70. Do not double-count a skill and an employer that prove the same thing.

Give each candidate a 1–2 sentence rationale citing the strongest signal, and short flags for risks/gaps."""

SET_RANKINGS_TOOL = {
    "name": "set_rankings",
    "description": "Record a fit score for every candidate provided.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "person_id": {"type": "string"},
                        "fit_score": {"type": "integer", "description": "0–100"},
                        "rationale": {"type": "string"},
                        "flags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["person_id", "fit_score"],
                },
            },
        },
        "required": ["rankings"],
    },
}

MAX_TOKENS = 4096


def _criteria_brief(c: Criteria) -> str:
    parts = [
        f"Title: {c.title or 'any'} (seniority: {c.seniority or 'any'})",
        f"YoE: {c.yoe_min if c.yoe_min is not None else '-'}–{c.yoe_max if c.yoe_max is not None else '-'}",
        f"Location: {c.location or c.location_country or 'any'}{' (remote ok)' if c.remote_ok else ''}",
        f"Must-have skills: {', '.join(c.must_have_skills) or 'none'}",
        f"Nice-to-have: {', '.join(c.nice_to_have_skills) or 'none'}",
        f"Domain: {', '.join(c.domain_signals) or 'none'}",
        f"Excludes: titles={', '.join(c.title_excludes) or 'none'}; employers={', '.join(c.exclude_employers) or 'none'}",
    ]
    if c.anchor_companies:
        parts.append(f"Target/peer companies: {', '.join(c.anchor_companies)}")
    return "\n".join(parts)


def _candidate_brief(cand: dict) -> dict:
    return {
        "person_id": str(cand.get("person_id") or ""),
        "name": cand.get("name") or "",
        "current_title": cand.get("current_title") or "",
        "current_company": cand.get("current_company") or "",
        "yoe": cand.get("yoe"),
        "region": cand.get("region") or "",
        "top_skills": (cand.get("top_skills") or [])[:12],
        "prior_employers": (cand.get("prior_employers") or [])[:3],
        "summary": (cand.get("summary") or "")[:400],
    }


def rank(candidates: list[dict], criteria: Criteria, top_n: int | None = None) -> list[dict]:
    """Re-score the top N candidates with an Opus judgment pass and re-sort them
    above the remainder. Returns the (possibly) reordered list; on any failure
    returns the input unchanged."""
    if not candidates or not llm.available():
        return candidates
    n = top_n or config.SMART_RANK_TOP_N
    head, tail = candidates[:n], candidates[n:]
    briefs = [_candidate_brief(c) for c in head if c.get("person_id")]
    if not briefs:
        return candidates

    import json
    user = (f"CRITERIA:\n{_criteria_brief(criteria)}\n\n"
            f"CANDIDATES (score every one by person_id):\n{json.dumps(briefs)}")
    try:
        client = llm.client()
        resp = client.messages.create(
            model=config.RANK_MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[SET_RANKINGS_TOOL],
            tool_choice={"type": "tool", "name": "set_rankings"},
            messages=[{"role": "user", "content": user}],
        )
    except Exception:
        return candidates  # fail-soft: keep deterministic order

    scores: dict[str, dict] = {}
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "set_rankings":
            for r in (block.input.get("rankings") or []):
                pid = str(r.get("person_id") or "")
                if pid:
                    scores[pid] = r
    if not scores:
        return candidates

    rescored = []
    for c in head:
        r = scores.get(str(c.get("person_id")))
        if r and isinstance(r.get("fit_score"), int):
            c = {**c, "score": max(0, min(100, r["fit_score"])),
                 "rationale": r.get("rationale") or c.get("rationale", ""),
                 "flags": r.get("flags") or c.get("flags", []),
                 "smart_ranked": True}
        rescored.append(c)
    rescored.sort(key=lambda c: (-c.get("score", 0), c.get("crustdata_rank", 0)))
    return rescored + tail
