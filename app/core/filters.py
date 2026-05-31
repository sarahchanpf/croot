"""Criteria -> Crustdata filter payload.

The single canonical filter builder (the one lesson kept from v1). Emits the
skill's envelope: {"op": "and", "conditions": [...]}, where the only nested
$or groups are the anchor clauses and multi-variant title matches.

Crustdata operator set (do NOT invent others — there is no substring-negation):
    [.]   substring        (.)   fuzzy
    =  !=  exact            in  not_in   set membership (value MUST be a list)
    >  <  >=  <=  numeric/date          geo_distance

Hard rule from the skill: never pass a guessed `company_industry` or
`education_background.institute_name` enum value — resolve it through
crustdata.autocomplete first, or the clause silently matches nothing.
"""

from __future__ import annotations

from .criteria import Criteria


class FIELD:
    CURRENT_TITLE = "current_employers.title"
    CURRENT_COMPANY_ID = "current_employers.company_id"
    PAST_COMPANY_ID = "past_employers.company_id"
    CURRENT_INDUSTRY = "current_employers.company_industry"
    PAST_INDUSTRY = "past_employers.company_industry"
    YEARS_AT_COMPANY = "current_employers.years_at_company_raw"
    YOE = "years_of_experience_raw"
    REGION = "region"
    COUNTRY = "location_country"
    SKILLS = "skills"
    SCHOOL = "education_background.institute_name"
    FIELD_OF_STUDY = "education.field_of_study"


def cond(column: str, type_: str, value) -> dict:
    return {"column": column, "type": type_, "value": value}


def op_or(conditions: list) -> dict:
    return {"op": "or", "conditions": conditions}


def op_and(conditions: list) -> dict:
    return {"op": "and", "conditions": conditions}


def build_filters(criteria: Criteria, hiring_company_id: int | None = None) -> dict:
    """Assemble the Crustdata filter payload for a search.

    TODO(impl): port the skill's per-clause construction:
      - title variants  -> [.] clauses under an OR
      - same-employer dedup + anti-cluster -> current_employers.company_id not_in [...]
      - location        -> region geo_distance OR location_country =
      - YoE band         -> years_of_experience_raw >= / <= (AND pair)
      - must-have skills -> skills in [variants...]  (drop if nice-to-have only)
      - anchor $or       -> company_id in [...] across current+past, and/or industry in [...]
      - education        -> field_of_study [.] OR-block; institute_name in [enum...]
      - tenure floor     -> years_at_company_raw >= months/12
    `title_excludes` is NOT emitted here — it's a local post-filter in ranker.
    """
    raise NotImplementedError("filters.build_filters — see TODO")
