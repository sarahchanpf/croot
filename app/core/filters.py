"""Criteria -> Crustdata filter payload.

The single canonical filter builder (the one lesson kept from v1). Emits the
skill's envelope: {"op": "and", "conditions": [...]}, where the only nested
$or groups are the anchor clauses and multi-variant title matches.

Crustdata operator set (do NOT invent others — there is no substring-negation):
    [.]   substring        (.)   fuzzy
    =  !=  exact            in  not_in   set membership (value MUST be a list)
    =>  =<  numeric/date comparison      geo_distance
    (NB: Crustdata spells the comparisons "=>" / "=<", NOT ">=" / "<=" — the
    latter is rejected with "Unknown operator type". Verified against the live
    API; matches v1's gte/lte helpers.)

Design: this is a PURE function — it makes no API calls, so it's fully unit-
testable without a key. Anything that needs Crustdata to resolve (company
name -> company_id, industry/school -> enum value) is done by the caller and
passed in via `Resolved`. The hard rule from the skill — never pass a guessed
`company_industry` / `institute_name` enum value — is therefore enforced at the
call site (resolve first), not here.

Skills semantics (deliberate, ported from the skill): all must-have skills go
into ONE `skills in [...]` clause, i.e. "has at least one of these". Strictness
("has more of them") is enforced by the 0-100 ranker, NOT by AND-ing skill
clauses at the filter level — that's what zeroed out v1's results and is why the
skill relaxes the skills clause first. `nice_to_have_skills` never filter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from .criteria import ANCHOR_STRATEGIES, Criteria

# Caps ported from the skill's anchor strategy (keep the net from over-widening).
ANCHOR_COMPANIES_CAP = 15
ANCHOR_COMPANIES_CAP_BOTH = 10
ANCHOR_INDUSTRIES_CAP = 4


class FIELD:
    CURRENT_TITLE = "current_employers.title"
    CURRENT_COMPANY_ID = "current_employers.company_id"
    PAST_COMPANY_ID = "past_employers.company_id"
    CURRENT_INDUSTRY = "current_employers.company_industries"
    PAST_INDUSTRY = "past_employers.company_industries"
    YEARS_AT_COMPANY = "current_employers.years_at_company_raw"
    YOE = "years_of_experience_raw"
    REGION = "region"
    COUNTRY = "location_country"
    SKILLS = "skills"
    SCHOOL = "education_background.institute_name"
    FIELD_OF_STUDY = "education_background.field_of_study"


@dataclass
class Resolved:
    """API-resolved inputs the pure builder can't compute itself.

    A field left at its default means "caller didn't resolve it":
      * id lists default to [] (no clause emitted),
      * enum lists default to None, which falls back to the raw criteria values
        (handy for tests and when intake already supplied clean enums).
    """
    hiring_company_id: int | None = None
    anchor_company_ids: list[int] = field(default_factory=list)
    exclude_company_ids: list[int] = field(default_factory=list)
    anchor_industries: list[str] | None = None   # autocompleted enum values
    schools: list[str] | None = None             # autocompleted enum values


# ---------- primitives ----------

def cond(column: str, type_: str, value) -> dict:
    return {"column": column, "type": type_, "value": value}


def op_or(conditions: list) -> dict:
    return {"op": "or", "conditions": conditions}


def op_and(conditions: list) -> dict:
    return {"op": "and", "conditions": conditions}


def _dedupe(values, *, lower_key: bool = True) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        key = v.lower() if lower_key else v
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


# ---------- per-stanza helpers (each returns 0+ conditions) ----------

def _title_conditions(criteria: Criteria) -> list:
    titles = _dedupe([criteria.title, *criteria.title_variants])
    if not titles:
        return []
    clauses = [cond(FIELD.CURRENT_TITLE, "[.]", t) for t in titles]
    return [clauses[0] if len(clauses) == 1 else op_or(clauses)]


def _location_conditions(criteria: Criteria, geo_radius_miles: int) -> list:
    if criteria.remote_ok:
        return []  # remote-friendly role: don't pin geography
    loc = criteria.location.strip()
    if loc:
        # geo_distance geocodes the location server-side, but a BARE city
        # ("Chicago") geocodes unreliably and silently fails OPEN — it matched
        # ~17M people worldwide in testing, so the geo filter became a no-op and
        # results came back from India/Colombia. Only trust geo_distance when the
        # string is qualified ("City, State" / "City, Country", e.g.
        # "Chicago, IL"); for a bare city fall back to a region SUBSTRING match,
        # which fails closed (a few correct hits, never a global dump).
        if "," in loc:
            return [cond(FIELD.REGION, "geo_distance", {
                "location": loc, "distance": geo_radius_miles, "unit": "mi",
            })]
        return [cond(FIELD.REGION, "[.]", loc)]
    if criteria.location_country.strip():
        return [cond(FIELD.COUNTRY, "=", criteria.location_country.strip())]
    return []


def _yoe_conditions(criteria: Criteria) -> list:
    out = []
    if criteria.yoe_min is not None:
        out.append(cond(FIELD.YOE, "=>", criteria.yoe_min))
    if criteria.yoe_max is not None:
        out.append(cond(FIELD.YOE, "=<", criteria.yoe_max))
    return out


def _skills_conditions(criteria: Criteria) -> list:
    skills = _dedupe(criteria.must_have_skills)
    if not skills:
        return []
    return [cond(FIELD.SKILLS, "in", skills)]


def _education_conditions(criteria: Criteria, resolved: Resolved) -> list:
    out = []
    majors = _dedupe(criteria.education.majors)
    if majors:
        clauses = [cond(FIELD.FIELD_OF_STUDY, "[.]", m) for m in majors]
        out.append(clauses[0] if len(clauses) == 1 else op_or(clauses))
    schools = resolved.schools if resolved.schools is not None else criteria.education.schools
    schools = _dedupe(schools, lower_key=False)
    if schools:
        out.append(cond(FIELD.SCHOOL, "in", schools))
    return out


def _effective_strategy(criteria: Criteria, company_ids: list[int], industries: list[str]) -> str:
    strategy = criteria.anchor_strategy if criteria.anchor_strategy in ANCHOR_STRATEGIES else "none"
    # Forgiving: if the caller left the default but supplied anchors, infer it
    # so anchoring isn't silently dropped.
    if strategy == "none" and (company_ids or industries):
        if company_ids and industries:
            strategy = "both"
        elif company_ids:
            strategy = "companies"
        else:
            strategy = "industries"
    return strategy


def _anchor_company_ids_and_industries(criteria: Criteria, resolved: Resolved):
    company_ids = sorted(set(resolved.anchor_company_ids))
    industries = resolved.anchor_industries if resolved.anchor_industries is not None else criteria.anchor_industries
    return company_ids, _dedupe(industries, lower_key=False)


def _anchor_conditions(criteria: Criteria, resolved: Resolved) -> list:
    company_ids, industries = _anchor_company_ids_and_industries(criteria, resolved)

    strategy = _effective_strategy(criteria, company_ids, industries)
    if strategy == "none":
        return []

    use_companies = strategy in ("companies", "both") and company_ids
    use_industries = strategy in ("industries", "both") and industries
    if not (use_companies or use_industries):
        return []

    cap = ANCHOR_COMPANIES_CAP_BOTH if strategy == "both" else ANCHOR_COMPANIES_CAP
    or_clauses: list = []
    if use_companies:
        ids = company_ids[:cap]
        or_clauses.append(cond(FIELD.CURRENT_COMPANY_ID, "in", ids))
        or_clauses.append(cond(FIELD.PAST_COMPANY_ID, "in", ids))
    if use_industries:
        inds = industries[:ANCHOR_INDUSTRIES_CAP]
        or_clauses.append(cond(FIELD.CURRENT_INDUSTRY, "in", inds))
        or_clauses.append(cond(FIELD.PAST_INDUSTRY, "in", inds))

    # "both" unifies under one outer $or (match any target company OR industry).
    return [or_clauses[0] if len(or_clauses) == 1 else op_or(or_clauses)]


def _exclusion_conditions(criteria: Criteria, resolved: Resolved) -> list:
    # Same-employer dedup (hiring company) merged with anti-cluster excludes,
    # both on current_employers.company_id via one not_in.
    exclude_ids: list[int] = list(resolved.exclude_company_ids)
    if resolved.hiring_company_id is not None:
        exclude_ids.append(resolved.hiring_company_id)
    exclude_ids = sorted(set(exclude_ids))
    if not exclude_ids:
        return []
    return [cond(FIELD.CURRENT_COMPANY_ID, "not_in", exclude_ids)]


def _tenure_conditions(criteria: Criteria) -> list:
    months = criteria.tenure_floor_months
    if not months or months <= 0:
        return []
    return [cond(FIELD.YEARS_AT_COMPANY, "=>", round(months / 12, 2))]


# ---------- public builder ----------

def build_filters(
    criteria: Criteria,
    resolved: Resolved | None = None,
    geo_radius_miles: int = config.GEO_RADIUS_DEFAULT_MILES,
) -> dict:
    """Assemble the Crustdata filter payload. Pure function.

    `title_excludes` is intentionally NOT emitted here — Crustdata has no
    substring-negation operator, so it's applied as a local post-filter in the
    ranker against each candidate's current title.
    """
    resolved = resolved or Resolved()

    # "Strong" narrowers define the pool. Skills are deliberately NOT a strong
    # filter: Crustdata's skills data is sparse, so `skills in [...]` wrongly
    # excludes qualified people. Must-have skills instead drive the 0-100 score
    # (search wide, rank precisely). The only exception is a skills-only search,
    # where we fall back to filtering on skills so we don't scan the whole DB.
    # (This matches the skill: it adds the skills clause only when must-haves are
    # a true hard requirement AND won't over-narrow, and drops it under a
    # title + anchor — which is the common case here.)
    #
    # TITLE is the full title-variants substring clause (skill Phase 2 Step 3) —
    # the same exact-phrase filter whether or not the search is company-anchored.
    # Broadening the title is a relaxation pass (search route), not the default.
    strong: list = []
    strong += _title_conditions(criteria)
    strong += _location_conditions(criteria, geo_radius_miles)
    strong += _anchor_conditions(criteria, resolved)
    strong += _education_conditions(criteria, resolved)

    conditions: list = list(strong)
    conditions += _yoe_conditions(criteria)
    conditions += _exclusion_conditions(criteria, resolved)
    conditions += _tenure_conditions(criteria)
    if not strong:
        conditions += _skills_conditions(criteria)  # fallback: nothing else to search on

    return op_and(conditions)
