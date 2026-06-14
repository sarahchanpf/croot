"""Candidate ranking — the skill's Phase 6: a 0-100 fit score per candidate.

The PRIMARY path is a single Opus reasoning pass (exactly what the skill does):
each candidate is scored 0-100 on fit to the confirmed criteria, judged
semantically rather than by string overlap. A DETERMINISTIC rubric is the
fallback when no ANTHROPIC_API_KEY is configured or the LLM call fails — so
dev/offline and the unit tests keep working without credits, and a flaky model
call never sinks a search.

Pre-scoring (deterministic, also matches the skill):
  * same-employer dedup — drop stale DB rows where the candidate already moved
    to the hiring company;
  * title_excludes post-filter — Crustdata has no substring-negation operator,
    so excluded title substrings are dropped locally.
Then sort by fit score desc, tiebreaking on the original Crustdata rank.

The cluster-pedigree slot / tier-sort / current-company floor that v2 carried
are GONE — the skill's Phase 6 rubric has no such slot. Cluster relevance is
already enforced upstream by the anchor `$or` filter and rewarded by the domain
slot, so peers surface without forcing them above higher-fit candidates.

This module also owns the relaxation *policy* (which single criterion to loosen
when the pool is thin); the search route applies it and re-queries.
"""

from __future__ import annotations

import re

from .. import config, llm
from . import regions
from .criteria import Criteria

W = config.RUBRIC_WEIGHTS
NEUTRAL_SCORE = 70   # used when no scored slot applies (e.g. anchor-only search)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _contains_any(needle: str, haystacks) -> bool:
    n = (needle or "").lower()
    return bool(n) and any(n in (h or "").lower() for h in haystacks)


# ======================================================================
# Public entry point
# ======================================================================

def rank(candidates: list[dict], criteria: Criteria, hiring_company_id: int | None = None) -> list[dict]:
    """Drop same-employer + title_excludes rows, score 0-100, sort desc.

    Scoring is the LLM pass when available, else the deterministic rubric. Each
    returned candidate carries `score` (int 0-100), `rationale` (str) and
    `flags` (list[str]) on top of its compressed-pool fields.
    """
    excludes = [t.strip().lower() for t in criteria.title_excludes if t and t.strip()]
    kept: list[dict] = []
    for cand in candidates:
        if hiring_company_id is not None and cand.get("current_company_id") == hiring_company_id:
            continue
        current_title = (cand.get("current_title") or "").lower()
        if excludes and any(x in current_title for x in excludes):
            continue
        kept.append(cand)

    scores = _score_pool(kept, criteria)
    out = [{**cand, **scores[i]} for i, cand in enumerate(kept)]
    out.sort(key=lambda c: (-c["score"], c.get("crustdata_rank", 0)))
    return out


def _score_pool(cands: list[dict], criteria: Criteria) -> list[dict]:
    """Return a parallel list of {score, rationale, flags} for `cands`.

    LLM pass when a key is set; deterministic rubric otherwise. The LLM path is
    self-healing: any candidate the model fails to score falls back to the
    deterministic rubric for that row, and a hard failure falls back wholesale.
    """
    if not cands:
        return []
    if llm.available():
        try:
            return _llm_score_pool(cands, criteria)
        except Exception:
            pass  # fall through to deterministic scoring — never sink a search
    return [score_one(cand, criteria) for cand in cands]


# ======================================================================
# LLM scoring (primary) — single reasoning pass, batched
# ======================================================================

RANK_BATCH_SIZE = 25   # skill batches ~25 to keep each reasoning pass focused

SYSTEM_PROMPT = """You are an expert technical recruiter scoring sourced candidates against a role's confirmed criteria. Score each candidate 0-100 on FIT to the criteria — not on absolute prestige.

Weight bands (a guide, bend them when one signal clearly dominates):
- Title / role match (current title vs. the target title + seniority): ~25
- Must-have skills overlap: ~25
- Domain / industry fit (current + past employers in relevant industries; weight recency): ~20
- Years-of-experience / seniority band match: ~15
- Location match (remote-friendly = full credit if the role allows it): ~10
- Bonus (recent transition into relevant work, relevant education, certs/honors): ~5

Rules:
- A slot the criteria don't specify is not scored — don't penalize its absence.
- Don't double-count: a relevant skill and a relevant employer that prove the same thing don't both stack.
- Crustdata skills/industry data is sparse: missing skills means UNKNOWN, not absent — don't zero a candidate just because a tag is absent.
- Penalize hard misses: a profile that contradicts an exclude (wrong sub-specialty, wrong domain, wrong geo when on-site only) caps at ~40 regardless of other signals.
- A candidate flagged data_gap (incomplete profile) caps at ~70 unless the surfaced fields independently justify more.

For each candidate return: its index, an integer score 0-100, a 1-2 sentence rationale citing the strongest signal driving the score, and 0-3 short flag tags for risks/gaps (e.g. "sub-specialty mismatch", "location mismatch", "years unknown", "incomplete profile")."""

SCORE_TOOL = {
    "name": "score_candidates",
    "description": "Record the 0-100 fit score, rationale, and risk flags for every candidate in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The candidate's index from the input list."},
                        "score": {"type": "integer", "description": "Fit score, 0-100."},
                        "rationale": {"type": "string", "description": "1-2 sentences citing the strongest signal."},
                        "flags": {"type": "array", "items": {"type": "string"},
                                  "description": "0-3 short risk/gap tags."},
                    },
                    "required": ["index", "score", "rationale"],
                },
            },
        },
        "required": ["scores"],
    },
}


def _criteria_brief(criteria: Criteria) -> str:
    """A compact, readable rendering of the confirmed criteria block."""
    lines = [f"Title: {criteria.title or 'unspecified'}"]
    if criteria.title_variants:
        lines.append(f"Title variants: {', '.join(criteria.title_variants)}")
    if criteria.seniority:
        lines.append(f"Seniority: {criteria.seniority}")
    if criteria.yoe_min is not None or criteria.yoe_max is not None:
        lines.append(f"Years of experience: {criteria.yoe_min if criteria.yoe_min is not None else 'any'}"
                     f"-{criteria.yoe_max if criteria.yoe_max is not None else 'any'}")
    if criteria.remote_ok:
        lines.append("Location: remote-friendly")
    elif criteria.location or criteria.location_country:
        lines.append(f"Location: {criteria.location or criteria.location_country}")
    if criteria.must_have_skills:
        lines.append(f"Must-have skills: {', '.join(criteria.must_have_skills)}")
    if criteria.nice_to_have_skills:
        lines.append(f"Nice-to-have skills: {', '.join(criteria.nice_to_have_skills)}")
    if criteria.domain_signals:
        lines.append(f"Domain / industry: {', '.join(criteria.domain_signals)}")
    if criteria.education.majors:
        lines.append(f"Education majors: {', '.join(criteria.education.majors)}")
    if criteria.education.schools:
        lines.append(f"Education schools: {', '.join(criteria.education.schools)}")
    if criteria.career_path_signals:
        lines.append(f"Career path: {', '.join(criteria.career_path_signals)}")
    if criteria.title_excludes:
        lines.append(f"Exclude titles: {', '.join(criteria.title_excludes)}")
    if criteria.exclude_employers:
        lines.append(f"Exclude employers: {', '.join(criteria.exclude_employers)}")
    return "\n".join(lines)


def _candidate_brief(cand: dict, index: int) -> dict:
    """The fields the model scores on, trimmed to keep the batch payload small."""
    return {
        "index": index,
        "current_title": cand.get("current_title") or "",
        "current_company": cand.get("current_company") or "",
        "current_seniority": cand.get("current_seniority") or "",
        "yoe": cand.get("yoe"),
        "region": cand.get("region") or "",
        "past_titles": (cand.get("titles") or [])[1:6],
        "prior_employers": cand.get("prior_employers") or [],
        "industries": cand.get("industries") or [],
        "top_skills": cand.get("top_skills") or [],
        "schools": cand.get("schools") or [],
        "summary": (cand.get("summary") or "")[:600],
        "data_gap": bool(cand.get("data_gap")),
    }


def _llm_score_pool(cands: list[dict], criteria: Criteria) -> list[dict]:
    """Score the whole pool via the LLM, batching to keep each pass focused.
    Any candidate the model omits falls back to the deterministic rubric."""
    import json

    results: list[dict | None] = [None] * len(cands)
    client = llm.client()
    brief = _criteria_brief(criteria)

    for start in range(0, len(cands), RANK_BATCH_SIZE):
        batch = cands[start:start + RANK_BATCH_SIZE]
        payload = [_candidate_brief(c, i) for i, c in enumerate(batch)]
        user = (
            "ROLE CRITERIA:\n" + brief +
            "\n\nCANDIDATES (score every one by its index):\n" +
            json.dumps(payload, ensure_ascii=False)
        )
        resp = client.messages.create(
            model=config.RANK_MODEL,
            max_tokens=config.RANK_MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[SCORE_TOOL],
            tool_choice={"type": "tool", "name": "score_candidates"},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "score_candidates":
                for row in (block.input.get("scores") or []):
                    idx = row.get("index")
                    if not isinstance(idx, int) or not (0 <= idx < len(batch)):
                        continue
                    results[start + idx] = _normalize_llm_row(row)

    # Self-heal: deterministic fallback for any candidate the model skipped.
    return [r if r is not None else score_one(cands[i], criteria)
            for i, r in enumerate(results)]


def _normalize_llm_row(row: dict) -> dict:
    try:
        score = int(round(float(row.get("score"))))
    except (TypeError, ValueError):
        score = NEUTRAL_SCORE
    score = max(0, min(100, score))
    flags = [str(f).strip() for f in (row.get("flags") or []) if str(f).strip()][:3]
    rationale = str(row.get("rationale") or "").strip()
    return {"score": score, "rationale": rationale, "flags": flags}


# ======================================================================
# Deterministic rubric (fallback) — per-slot fractional scoring
# ======================================================================
#
# A slot only counts toward the denominator when its criterion was actually
# requested, so a search that omits (say) skills isn't penalised for it:
#     score = 100 * earned_weight / applicable_weight.
# nice_to_have skills are a pure bonus (add to num AND den only on a match).

def _title_fraction(criteria: Criteria, cand: dict):
    terms = [t.strip() for t in [criteria.title, *criteria.title_variants] if t and t.strip()]
    if not terms:
        return None
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
        return 0.6                                   # strong current-title overlap
    if any(_contains_any(t, cand.get("titles") or []) for t in terms):
        return 0.4                                   # only a past title matched
    return 0.25 if best > 0 else 0.0                 # weak current overlap / none


def _skills_detail(criteria: Criteria, cand: dict):
    must = [s.strip() for s in criteria.must_have_skills if s and s.strip()]
    if not must:
        return None, [], []
    skills_l = [s.lower() for s in (cand.get("top_skills") or [])]
    if not skills_l:
        # Sparse Crustdata skills data: no listed skills means "unknown", not
        # "lacks them" — skip the slot rather than zeroing a thin profile.
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
    region_countries = regions.countries_for(criteria.location_region)
    if not (criteria.location.strip() or criteria.location_country.strip() or region_countries):
        return None
    region = cand.get("region") or ""
    if not region:
        return 0.0
    region_l = region.lower()
    if region_countries:
        # In-region (candidate's country is one of the region's) scores full.
        return 1.0 if any(c.lower() in region_l for c in region_countries) else 0.7
    city = (criteria.location.split(",")[0].strip()
            if criteria.location.strip() else criteria.location_country.strip())
    if city and city.lower() in region_l:
        return 1.0
    return 0.7   # passed the geo filter but the name didn't string-match


def score_one(cand: dict, criteria: Criteria) -> dict:
    """Deterministic 0-100 fallback score. Same rubric slots as the LLM pass,
    minus the semantic judgement. No cluster-pedigree slot (skill parity)."""
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

    if cand.get("data_gap"):
        flags.append("incomplete profile")
        score = min(score, config.CAP_DATA_GAP)
    if cand.get("yoe") is None and (criteria.yoe_min is not None or criteria.yoe_max is not None):
        flags.append("years unknown")

    rationale = "Matches " + ("; ".join(matched) if matched else "the search filters")
    if missed:
        rationale += ". Misses " + "; ".join(missed)
    rationale += "."

    return {"score": score, "rationale": rationale, "flags": flags}


# ======================================================================
# Relaxation policy (used by the search route)
# ======================================================================
#
# When the pool is thin (< config.BROAD_HEALTHY_TOTAL_COUNT) the search route
# applies ONE relaxation, the single highest-leverage loosening available. Order
# mirrors the skill's Phase 2 Step 4 list, adjusted for what v2 ACTUALLY filters
# on (title / geo / anchor / education / yoe — NOT skills, which are scoring-only
# here except in a skills-only search):
#   1. drop skills      — only meaningful in a skills-only search (the build_filters
#                         fallback); a no-op otherwise, so it's gated on that.
#   2. broaden title    — the top over-narrower under a company anchor. Live data:
#                         "Backend Engineer" at a fintech cluster in SF = 0 hits,
#                         but the head-noun "Engineer" = 71. Drop title_variants
#                         first, else reduce the title to its head noun so the
#                         cluster stays intact (vs. dropping the anchor, which the
#                         skill treats as the LAST resort).
#   3. widen geo
#   4. drop education
#   5. drop the anchor   — last resort.
# Returns (mutated criteria, geo radius, user-facing label) or (None, radius, None).

def _role_core(title: str) -> str:
    """The role's core noun (last word), e.g. 'Solutions Architect' -> 'Architect',
    'Backend Engineer' -> 'Engineer'. '' for an empty / too-short title."""
    words = re.findall(r"[A-Za-z]+", title or "")
    return words[-1] if words and len(words[-1]) >= 3 else ""


def plan_relaxation(criteria: Criteria, current_radius: int = config.GEO_RADIUS_DEFAULT_MILES):
    c = Criteria.from_dict(criteria.to_dict())

    # 1. Skills filter only exists in a skills-only search (build_filters fallback);
    #    elsewhere skills are scoring-only, so dropping them wouldn't change the pool.
    skills_only = not (c.title.strip() or c.title_variants or c.location.strip()
                       or c.location_country.strip() or c.anchor_companies
                       or c.anchor_industries or c.education.majors or c.education.schools)
    if skills_only and c.must_have_skills:
        c.must_have_skills = []
        return c, current_radius, "dropped must-have skills"

    # 2. Broaden the title — reduce every title form (base + variants) to its role
    #    core (head noun). This is strictly BROADER than the exact phrases (every
    #    prior match still matches, plus more) and keeps the anchor cluster intact.
    #    NB: simply dropping the variants would NARROW the title OR, not broaden it
    #    (it removes alternatives), which is why a thin pool got thinner.
    titles = [c.title, *c.title_variants]
    cores: list[str] = []
    seen_cores: set[str] = set()
    for t in titles:
        core = _role_core(t)
        if core and core.lower() not in seen_cores:
            seen_cores.add(core.lower())
            cores.append(core)
    current_titles = {t.strip().lower() for t in titles if t.strip()}
    if cores and {core.lower() for core in cores} != current_titles:
        c.title = cores[0]
        c.title_variants = cores[1:]
        return c, current_radius, f"broadened title to role ({', '.join(cores)})"

    # 3. Widen geo.
    if ((c.location.strip() or c.location_country.strip()) and not c.remote_ok
            and current_radius < config.GEO_RADIUS_BROAD_MILES):
        return c, config.GEO_RADIUS_BROAD_MILES, "widened search radius to 100mi"

    # 4. Drop education.
    if c.education.majors or c.education.schools:
        c.education.majors = []
        c.education.schools = []
        return c, current_radius, "dropped education filters"

    # 5. Drop the anchor (last resort).
    if c.anchor_companies or c.anchor_industries or c.anchor_strategy != "none":
        c.anchor_companies = []
        c.anchor_industries = []
        c.anchor_strategy = "none"
        return c, current_radius, "dropped the company/industry anchor"

    return None, current_radius, None
