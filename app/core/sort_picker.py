"""Pick a Crustdata `sorts` axis for a search.

Ported from the skill's references/sort-recipes.md. Crustdata's DB response is
UNSORTED by default (server default is `person_id asc`), so fetching `limit` of
a large matching set returns an arbitrary slice — the top of the list is noise.
Sorting on the role's clearest axis makes the fetched sample (and therefore the
downstream ranking) meaningful instead of random.

Returns a list of {column, order} (max 2) or None when no axis is clear. The
columns/syntax are the vetted, sortable set from sort-recipes.md (verified
against the live legacy REST API).
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
# Explicit seniority lock. Per the skill, a YoE *floor* alone does NOT count as
# a seniority axis — the title/seniority must literally say senior+.
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
    """Choose the `sorts` array for this search, or None for no clear axis.

    Preserve the return value through a relaxation pass (sort-recipes hard rule):
    the search route reuses it on the relaxed re-query unless the relaxation drops
    the clause the axis depended on.
    """
    text = " ".join([
        (criteria.title or ""), " ".join(criteria.title_variants or []),
        (criteria.seniority or ""),
    ]).lower()

    # GTM / public-facing: network strength leads. (Checked first so a
    # "Senior Account Executive" sorts by connections, not raw YoE.)
    if _has_token(text, _GTM_TOKENS):
        return [{"column": _CONNECTIONS, "order": "desc"},
                {"column": _YOE, "order": "desc"}]

    # Junior / ramp with a YoE ceiling: least-experienced first, recent starts
    # next. Checked before the seniority lock so a low ceiling wins even if the
    # title says e.g. "lead" loosely. Never emit YoE asc without a ceiling (it
    # would surface floor-clearers, per the hard rule).
    if criteria.yoe_max is not None and criteria.yoe_max <= 5:
        return [{"column": _YOE, "order": "asc"},
                {"column": _START, "order": "desc"}]

    # Explicit seniority lock: most-experienced first, tenure as tiebreaker.
    if _has_token(text, _SENIOR_TOKENS):
        return [{"column": _YOE, "order": "desc"},
                {"column": _TENURE, "order": "desc"}]

    # No clear axis — let Crustdata default (caller logs this).
    return None
