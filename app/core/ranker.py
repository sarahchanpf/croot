"""Candidate ranking — deterministic 0-100 rubric (no LLM, no credits).

Ported from the skill's Phase 6. Each candidate is scored on fit to the
criteria using config.RUBRIC_WEIGHTS. A slot only counts toward the
denominator when its criterion was actually requested, so a search that omits
(say) skills isn't penalised for it — the score is
    100 * earned_weight / applicable_weight.

nice_to_have skills are a bonus: they add to both numerator and denominator
only on a match, so they lift the score and never drag it down (v1's rule).

Hard-miss caps (config): data_gap rows cap at CAP_DATA_GAP; a candidate whose
current title contradicts a title_exclude caps at CAP_CONTRADICTS_EXCLUDE
(defensive — rank() also drops those outright).

This module also owns the relaxation *policy* (which single criterion to loosen
when the pool is thin); the search route applies it and re-queries.
"""

from __future__ import annotations

import re

from .. import config
from .criteria import Criteria

W = config.RUBRIC_WEIGHTS
NEUTRAL_SCORE = 70   # used when no scored slot applies (e.g. anchor-only search)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _contains_any(needle: str, haystacks) -> bool:
    n = (needle or "").lower()
    return bool(n) and any(n in (h or "").lower() for h in haystacks)


# ---------- per-slot scorers: each returns a 0..1 fraction or None (n/a) ----------

def _title_fraction(criteria: Criteria, cand: dict):
    terms = [t.strip() for t in [criteria.title, *criteria.title_variants] if t and t.strip()]
    if not terms:
        return None
    # When a company cluster anchors the pool, title is the primary role signal,
    # so it must discriminate well. Grade by CURRENT-title fit, with only modest
    # credit for a stale (past-only) match so career-changers don't tie with
    # current ICs.
    current = (cand.get("current_title") or "").lower()
    if any(t.lower() in current for t in terms):
        return 1.0                                   # exact role in current title
    cur_words = _words(current)
    best = 0.0
    for t in terms:
        tw = _words(t)
        if tw and cur_words:
            best = max(best, len(tw & cur_words) / len(tw))
    if best >= 0.5:
        return 0.6                                   # strong current-title overlap (e.g. "Senior Software Engineer")
    if any(_contains_any(t, cand.get("titles") or []) for t in terms):
        return 0.4                                   # only a past title matched
    return 0.25 if best > 0 else 0.0                 # weak current overlap / none


def _skills_detail(criteria: Criteria, cand: dict):
    must = [s.strip() for s in criteria.must_have_skills if s and s.strip()]
    if not must:
        return None, [], []
    skills_l = [s.lower() for s in (cand.get("top_skills") or [])]
    if not skills_l:
        # Crustdata skills data is sparse — no listed skills means "unknown",
        # not "lacks them". Skip the slot so it neither credits nor penalises,
        # rather than zeroing out everyone with a thin profile.
        return None, [], []
    matched = [s for s in must if any(s.lower() in sk for sk in skills_l)]
    missed = [s for s in must if s not in matched]
    return len(matched) / len(must), matched, missed


def _domain_fraction(criteria: Criteria, cand: dict):
    sig = [d.strip().lower() for d in criteria.domain_signals if d and d.strip()]
    if not sig:
        return None
    hay = (" ".join(cand.get("industries") or []) + " " + (cand.get("summary") or "")).lower()
    return sum(1 for d in sig if d in hay) / len(sig)


def _yoe_seniority_fraction(criteria: Criteria, cand: dict):
    parts: list[float] = []
    if criteria.yoe_min is not None or criteria.yoe_max is not None:
        yoe = cand.get("yoe")
        if yoe is None:
            parts.append(0.0)
        else:
            ok = True
            if criteria.yoe_min is not None and yoe < criteria.yoe_min:
                ok = False
            if criteria.yoe_max is not None and yoe > criteria.yoe_max:
                ok = False
            parts.append(1.0 if ok else 0.0)
    if criteria.seniority.strip():
        target = criteria.seniority.strip().lower()
        sen = (cand.get("current_seniority") or "").lower()
        parts.append(1.0 if sen and (target in sen or sen in target) else 0.0)
    if not parts:
        return None
    return sum(parts) / len(parts)


def _location_fraction(criteria: Criteria, cand: dict):
    if criteria.remote_ok:
        return None
    if not (criteria.location.strip() or criteria.location_country.strip()):
        return None
    region = cand.get("region") or ""
    if not region:
        return 0.0
    city = (criteria.location.split(",")[0].strip()
            if criteria.location.strip() else criteria.location_country.strip())
    if city and city.lower() in region.lower():
        return 1.0
    return 0.7   # passed the geo filter but the name didn't string-match


# ---------- scoring ----------

def score_one(cand: dict, criteria: Criteria) -> dict:
    num = 0.0
    den = 0.0
    matched: list[str] = []
    missed: list[str] = []
    flags: list[str] = []

    def slot(key: str, frac, label: str):
        nonlocal num, den
        if frac is None:
            return
        num += W[key] * frac
        den += W[key]
        (matched if frac >= 0.5 else missed).append(label)

    slot("title", _title_fraction(criteria, cand), "title")

    skills_frac, sk_matched, sk_missed = _skills_detail(criteria, cand)
    if skills_frac is not None:
        num += W["skills"] * skills_frac
        den += W["skills"]
        if sk_matched:
            matched.append("skills: " + ", ".join(sk_matched))
        if sk_missed:
            missed.append("skills: " + ", ".join(sk_missed))

    slot("domain", _domain_fraction(criteria, cand), "domain")
    slot("yoe_seniority", _yoe_seniority_fraction(criteria, cand), "experience/seniority")
    slot("location", _location_fraction(criteria, cand), "location")

    # Bonus (nice-to-have): lifts only — adds to num AND den only on a match.
    nice = [s.strip() for s in criteria.nice_to_have_skills if s and s.strip()]
    if nice:
        skills_l = [s.lower() for s in (cand.get("top_skills") or [])]
        hit = [s for s in nice if any(s.lower() in sk for sk in skills_l)]
        if hit:
            add = W["bonus"] * (len(hit) / len(nice))
            num += add
            den += add
            matched.append("bonus: " + ", ".join(hit))

    score = round(100 * num / den) if den > 0 else NEUTRAL_SCORE

    # Caps.
    if cand.get("data_gap"):
        flags.append("incomplete profile")
        score = min(score, config.CAP_DATA_GAP)
    if cand.get("yoe") is None and (criteria.yoe_min is not None or criteria.yoe_max is not None):
        flags.append("years unknown")

    rationale = "Matches " + ("; ".join(matched) if matched else "the search filters")
    if missed:
        rationale += ". Misses " + "; ".join(missed)
    rationale += "."

    return {"score": score, "rationale": rationale, "flags": flags,
            "matched": matched, "missed": missed}


def rank(candidates: list[dict], criteria: Criteria, hiring_company_id: int | None = None) -> list[dict]:
    """Score, drop same-employer matches and title_excludes, sort desc.

    Same-employer dedup handles stale DB rows where the candidate already moved
    to the hiring company. title_excludes is the local post-filter standing in
    for Crustdata's missing substring-negation operator.
    """
    excludes = [t.strip().lower() for t in criteria.title_excludes if t and t.strip()]
    out: list[dict] = []
    for cand in candidates:
        if hiring_company_id is not None and cand.get("current_company_id") == hiring_company_id:
            continue
        current_title = (cand.get("current_title") or "").lower()
        if excludes and any(x in current_title for x in excludes):
            continue
        out.append({**cand, **score_one(cand, criteria)})
    out.sort(key=lambda c: (-c["score"], c.get("crustdata_rank", 0)))
    return out


# ---------- relaxation policy ----------
#
# When the pool is thin (< config.BROAD_HEALTHY_TOTAL_COUNT) the search route
# applies ONE relaxation, picking the single highest-leverage loosening
# available, in this order: skills -> title variants -> geo -> education ->
# anchor. plan_relaxation returns the mutated criteria, the geo radius to use,
# and a user-facing label — or (None, radius, None) when nothing's left.

def plan_relaxation(criteria: Criteria, current_radius: int = config.GEO_RADIUS_DEFAULT_MILES):
    c = Criteria.from_dict(criteria.to_dict())
    if c.must_have_skills:
        c.must_have_skills = []
        return c, current_radius, "dropped must-have skills"
    if c.title_variants:
        c.title_variants = []
        return c, current_radius, "broadened title (dropped variants)"
    if ((c.location.strip() or c.location_country.strip()) and not c.remote_ok
            and current_radius < config.GEO_RADIUS_BROAD_MILES):
        return c, config.GEO_RADIUS_BROAD_MILES, "widened search radius to 100mi"
    if c.education.majors or c.education.schools:
        c.education.majors = []
        c.education.schools = []
        return c, current_radius, "dropped education filters"
    if c.anchor_companies or c.anchor_industries or c.anchor_strategy != "none":
        c.anchor_companies = []
        c.anchor_industries = []
        c.anchor_strategy = "none"
        return c, current_radius, "dropped the company/industry anchor"
    return None, current_radius, None
