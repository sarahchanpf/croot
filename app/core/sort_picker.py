"""Pick a Crustdata `sorts` axis for a search.

Ported from the skill's references/sort-recipes.md. Crustdata's response is
UNSORTED by default, so fetching `limit` of a large pool returns a random slice
— the top of the list is noise. Sorting on the role's clearest axis makes the
fetched sample (and therefore the ranking) meaningful and deterministic.

Returns a list of {column, order} (max 2) or None when no axis is clear.
Verified columns/syntax against the live legacy REST API.
"""

from __future__ import annotations

import re

from .criteria import Criteria

# Public-facing / GTM roles — network size is signal here (and ONLY here;
# it's an anti-signal for engineering ICs, so we never sort on it otherwise).
_GTM_TOKENS = (
    "sales", "account executive", "account manager", "business development",
    "bdr", "sdr", "customer success", "devrel", "developer advocate",
    "go-to-market", "gtm", "partnerships", "solutions engineer",
    "sales engineer", "solutions architect",
)
# Explicit seniority lock (a YoE floor alone does NOT count — per the skill).
_SENIOR_TOKENS = (
    "senior", "sr", "staff", "principal", "lead", "architect",
    "head", "director", "vp", "chief", "distinguished",
)

_YOE = "years_of_experience_raw"
_TENURE = "current_employers.years_at_company_raw"
_START = "current_employers.start_date"
_CONNECTIONS = "num_of_connections"


def _has_token(text: str, tokens) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens)


def pick_sorts(criteria: Criteria) -> list[dict] | None:
    text = " ".join([
        (criteria.title or ""), " ".join(criteria.title_variants or []),
        (criteria.seniority or ""),
    ]).lower()

    # GTM / public-facing: network strength leads. (Checked first so a
    # "Senior Account Executive" sorts by connections, not raw YoE.)
    if _has_token(text, _GTM_TOKENS):
        return [{"column": _CONNECTIONS, "order": "desc"},
                {"column": _YOE, "order": "desc"}]

    # Explicit seniority lock: most-experienced first, tenure as tiebreaker.
    if _has_token(text, _SENIOR_TOKENS):
        return [{"column": _YOE, "order": "desc"},
                {"column": _TENURE, "order": "desc"}]

    # Junior/ramp with a YoE ceiling: least-experienced first, recent starts next.
    # Never emit YoE asc without a ceiling (would surface floor-clearers).
    if criteria.yoe_max is not None and criteria.yoe_max <= 5:
        return [{"column": _YOE, "order": "asc"},
                {"column": _START, "order": "desc"}]

    # No clear axis — let Crustdata default (caller logs this).
    return None
