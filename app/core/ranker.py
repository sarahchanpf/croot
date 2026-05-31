"""Candidate ranking — deterministic 0-100 rubric (no LLM, no credits).

Ported from the skill's Phase 6. Each candidate is scored on fit to the
criteria using config.RUBRIC_WEIGHTS, with hard-miss caps applied. Also owns
the relaxation ladder used when the pool is too thin.
"""

from __future__ import annotations

from .criteria import Criteria


def score_one(candidate: dict, criteria: Criteria) -> dict:
    """Return {"score": 0..100, "rationale": str, "flags": [...],
    "matched": [...], "missed": [...]} for one compressed candidate.

    TODO(impl): weight per RUBRIC_WEIGHTS (title 25 / skills 25 / domain 20 /
    yoe_seniority 15 / location 10 / bonus 5). Apply caps: contradicts an
    exclude -> CAP_CONTRADICTS_EXCLUDE; data_gap -> CAP_DATA_GAP. Rationale is
    built deterministically from matched/missed slots.
    """
    raise NotImplementedError("ranker.score_one — see TODO")


def rank(candidates: list[dict], criteria: Criteria, hiring_company_id: int | None = None) -> list[dict]:
    """Score, drop same-employer matches and title_excludes, sort desc.

    TODO(impl): drop candidate if current company_id == hiring_company_id
    (stale-DB dedup) or current title matches any criteria.title_excludes
    (case-insensitive substring). Tiebreak by preserved Crustdata order.
    """
    raise NotImplementedError("ranker.rank — see TODO")


# Relaxation ladder: applied in order, ONE pass, when pool < BROAD_HEALTHY_TOTAL_COUNT.
# Pick the single highest-leverage relaxation available, surface what changed.
RELAXATION_LADDER = [
    ("must-have skills", "drop skills clause"),
    ("title", "broaden to the least-specific title variant"),
    ("location", "widen geo radius 50 -> 100mi"),
    ("education", "drop education clauses"),
    ("anchor", "drop the company/industry anchor $or"),
]
