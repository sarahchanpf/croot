"""Full-fat search response -> compressed candidate pool.

A 100-profile full-fat response is huge; this is a pure local projection down to
the fields we render and score. The compressed pool is the single source of
truth for everything downstream (ranking, cards, CSV export) — no further
Crustdata calls are needed to display results.

Field-access fallbacks mirror v1's `_profile_employers` / `_score_one_profile`,
which ran against real Crustdata responses.

Compressed candidate shape (keys consumed by ranker.py and export/csv_dest.py):
    person_id, name, linkedin_url, headline, region, yoe,
    current_company, current_company_id, current_title, current_seniority,
    current_start_date, current_tenure_years, titles[], top_skills[], prior_employers[],
    industries[], schools[], summary, crustdata_rank, data_gap
"""

from __future__ import annotations

import re

SUMMARY_MAX_CHARS = 1500
TOP_SKILLS = 20
TOP_PRIOR_EMPLOYERS = 3


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _to_int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Titles that are usually NOT a real job — community memberships, mentorships,
# advisory/board seats. Profiles often list several of these as "current
# employers"; they shouldn't be treated as the person's current role (and they
# let a brand-new real job slip past the tenure floor — e.g. a 2-month AE whose
# old "Member @ <community>" entry satisfies the 6-month floor).
_NON_JOB_TITLE_RE = re.compile(
    r"\b(member|mentor|advisor|adviser|advisory|volunteer|board\s+member)\b",
    re.IGNORECASE,
)


def _is_real_job(emp: dict) -> bool:
    title = _first(emp, "title", "employee_title")
    return bool(title) and not _NON_JOB_TITLE_RE.search(title)


def _primary_current(current: list[dict]) -> dict:
    """The candidate's primary current employer. Crustdata's `employer_is_default`
    flag wins (among real jobs); else the first real job; else the first entry.
    Skips community/advisory 'current employers' that aren't real jobs, so the
    displayed/scored current role is the actual job — not 'Member @ <group>'."""
    if not current:
        return {}
    real = [e for e in current if _is_real_job(e)] or current
    for e in real:
        if e.get("employer_is_default"):
            return e
    return real[0]


def compress(raw_profiles: list[dict], rank_offset: int = 0) -> list[dict]:
    out: list[dict] = []
    for i, p in enumerate(raw_profiles):
        current = p.get("current_employers") or []
        past = p.get("past_employers") or []
        cur = _primary_current(current)
        all_emp = current + past

        titles = [t for t in (_first(e, "title", "employee_title") for e in all_emp) if t]
        industries = [e.get("company_industry") for e in all_emp if e.get("company_industry")]
        prior = [n for n in (_first(e, "name", "employer_name") for e in past) if n][:TOP_PRIOR_EMPLOYERS]
        skills = [s for s in (p.get("skills") or []) if isinstance(s, str)][:TOP_SKILLS]
        schools = [s for s in (_first(ed, "institute_name", "school")
                               for ed in (p.get("education_background") or [])) if s]

        linkedin = _first(p, "linkedin_url", "linkedin_profile_url", "flagship_profile_url")
        name = _first(p, "name", "full_name")
        person_id = p.get("person_id") or p.get("id")

        cand = {
            "person_id": person_id,
            "name": name,
            "linkedin_url": linkedin,
            "headline": p.get("headline") or "",
            "region": _first(p, "region", "location"),
            "yoe": _to_int(p.get("years_of_experience_raw")),
            "current_company": _first(cur, "name", "employer_name"),
            "current_company_id": cur.get("company_id"),
            "past_company_ids": [e.get("company_id") for e in past if e.get("company_id")],
            "current_title": _first(cur, "title", "employee_title"),
            "current_seniority": cur.get("seniority_level") or "",
            "current_start_date": cur.get("start_date") or "",
            "current_tenure_years": _to_float(cur.get("years_at_company_raw")),
            "titles": titles,
            "top_skills": skills,
            "prior_employers": prior,
            "industries": industries,
            "schools": schools,
            "summary": (p.get("summary") or "")[:SUMMARY_MAX_CHARS],
            "crustdata_rank": rank_offset + i,
        }
        # data_gap: missing identity or employment makes a row unrenderable /
        # unscoreable with confidence — ranker caps its score.
        cand["data_gap"] = not (person_id and linkedin and (cand["current_company"] or cand["current_title"]))
        out.append(cand)
    return out
