"""Search endpoints.

POST /api/preview  {criteria}  -> {total_count}    (limit:1, cheap pre-spend check)
POST /api/search   {criteria}  -> {candidates, relaxed, total_count, mode}

Orchestration (the heart of the product):
    criteria -> resolve anchors -> build_filters -> crustdata.search (full-fat)
             -> if thin: ONE relaxation pass and re-search
             -> pool.compress -> ranker.rank (against the ORIGINAL criteria)
             -> cache + history.

Candidates are always scored against what the recruiter actually asked for,
even when the filter was relaxed to find them — so the relaxed clauses show up
as misses in the rationale and the `relaxed` list tells the UI what we loosened.
"""

import json

from flask import Blueprint, jsonify, request

from .. import config
from ..core import crustdata, pool, ranker
from ..core.criteria import Criteria
from ..core.crustdata import CrustdataError
from ..core.filters import Resolved, build_filters
from ..db import cache_key_for, get_cached, put_cached

bp = Blueprint("search", __name__)


def _resolve_anchors(criteria: Criteria) -> Resolved:
    """Resolve everything build_filters can't compute itself: company names ->
    ids (identify, cached) and industry/school -> enum values (autocomplete).
    All fail-soft, so an unresolvable name just drops that clause."""
    # anchor_companies is the final company list (the chat route already ran the
    # cluster builder to fill it from any cluster_hint).
    company_ids = [cid for c in criteria.anchor_companies if (cid := crustdata.identify(c))]
    exclude_ids = [cid for c in criteria.exclude_employers if (cid := crustdata.identify(c))]
    hiring_id = crustdata.identify(criteria.hiring_company) if criteria.hiring_company.strip() else None

    # Autocomplete field names are NEW-API names; the values they return are
    # filtered on the legacy search columns in filters.py (verified compatible).
    industries: list[str] = []
    for ind in criteria.anchor_industries:
        industries += crustdata.autocomplete("experience.employment_details.current.company_industries", ind)
    schools: list[str] = []
    for sch in criteria.education.schools:
        schools += crustdata.autocomplete("education.schools.school", sch)

    # Pass explicit lists (even empty) so build_filters uses exactly what
    # resolved — never falling back to raw, unresolved enum names.
    return Resolved(
        hiring_company_id=hiring_id,
        anchor_company_ids=company_ids,
        exclude_company_ids=exclude_ids,
        anchor_industries=industries,
        schools=schools,
    )


def _summarize(criteria: Criteria) -> str:
    parts = []
    if criteria.title:
        parts.append(criteria.title)
    if criteria.location:
        parts.append("in " + criteria.location)
    elif criteria.location_country:
        parts.append("in " + criteria.location_country)
    if criteria.must_have_skills:
        parts.append("must: " + ", ".join(criteria.must_have_skills))
    if criteria.anchor_companies:
        parts.append("ex-" + ", ".join(criteria.anchor_companies))
    return " · ".join(parts) or "Untitled search"


def _profile_key(profile: dict) -> str:
    """Stable key for merging profiles returned by multiple Crustdata passes."""
    for field in ("person_id", "id", "linkedin_url", "linkedin_profile_url", "flagship_profile_url"):
        value = profile.get(field)
        if value:
            return f"{field}:{str(value).strip().lower()}"
    current = profile.get("current_employers") or []
    cur = current[0] if current else {}
    fallback = "|".join([
        str(profile.get("name") or profile.get("full_name") or "").strip().lower(),
        str(cur.get("name") or cur.get("employer_name") or "").strip().lower(),
        str(cur.get("title") or cur.get("employee_title") or "").strip().lower(),
    ])
    return f"fallback:{fallback}"


def _merge_profiles(target: list[dict], seen: set[str], profiles: list[dict]) -> int:
    added = 0
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        key = _profile_key(profile)
        if key in seen:
            continue
        seen.add(key)
        target.append(profile)
        added += 1
    return added


def _without_anchor(criteria: Criteria) -> Criteria:
    c = Criteria.from_dict(criteria.to_dict())
    c.anchor_strategy = "none"
    c.anchor_companies = []
    c.anchor_industries = []
    c.cluster_hint = ""
    return c


def _broad_retrieval_criteria(criteria: Criteria) -> Criteria:
    c = _without_anchor(criteria)
    c.title_variants = []
    c.education.majors = []
    c.education.schools = []
    return c


@bp.route("/api/preview", methods=["POST"])
def preview():
    criteria = Criteria.from_dict(request.get_json(force=True, silent=True) or {})
    if criteria.is_empty():
        return jsonify({"error": "Add at least one criterion."}), 400

    resolved = _resolve_anchors(criteria)
    payload = build_filters(criteria, resolved)
    if not payload["conditions"]:
        return jsonify({"error": "Add at least one criterion."}), 400
    try:
        data = crustdata.search(payload, limit=1)
    except CrustdataError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify({"total_count": data.get("total_count") or 0})


@bp.route("/api/search", methods=["POST"])
def search():
    body = request.get_json(force=True, silent=True) or {}
    criteria = Criteria.from_dict(body)
    if criteria.is_empty():
        return jsonify({"error": "Add at least one criterion."}), 400

    resolved = _resolve_anchors(criteria)
    radius = config.GEO_RADIUS_DEFAULT_MILES
    payload = build_filters(criteria, resolved, geo_radius_miles=radius)
    if not payload["conditions"]:
        return jsonify({"error": "Add at least one criterion."}), 400

    limit = int(body.get("limit", config.SEARCH_LIMIT))
    cache_key = cache_key_for({
        "criteria": criteria.to_dict(),
        "limit": limit,
        "algo": config.SEARCH_ALGO_VERSION,
    })
    cached = get_cached(cache_key)
    if cached is not None:
        return jsonify({**cached, "from_cache": True})

    raw_profiles: list[dict] = []
    seen_profiles: set[str] = set()
    seen_payloads: set[str] = set()
    search_passes: list[dict] = []
    relaxed: list[str] = []

    def run_pass(label: str, pass_criteria: Criteria, pass_resolved: Resolved, pass_radius: int):
        if pass_criteria.is_empty():
            return None
        pass_payload = build_filters(pass_criteria, pass_resolved, geo_radius_miles=pass_radius)
        if not pass_payload["conditions"]:
            return None
        payload_key = json.dumps(pass_payload, sort_keys=True, separators=(",", ":"))
        if payload_key in seen_payloads:
            return None
        seen_payloads.add(payload_key)
        data = crustdata.search(pass_payload, limit=limit)
        added = _merge_profiles(raw_profiles, seen_profiles, data.get("profiles") or [])
        search_passes.append({
            "label": label,
            "total_count": data.get("total_count") or 0,
            "returned": len(data.get("profiles") or []),
            "added": added,
        })
        return data

    try:
        data = run_pass("company cluster" if resolved.anchor_company_ids else "primary", criteria, resolved, radius)
        if data is None:
            return jsonify({"error": "Add at least one criterion."}), 400

        if len(raw_profiles) < config.TARGET_MERGED_POOL_SIZE and resolved.anchor_company_ids:
            expanded = _without_anchor(criteria)
            expanded_resolved = _resolve_anchors(expanded)
            if run_pass("expanded beyond company cluster", expanded, expanded_resolved, radius):
                relaxed.append("expanded beyond company cluster")
        elif len(raw_profiles) < config.TARGET_MERGED_POOL_SIZE:
            relaxed_criteria, new_radius, label = ranker.plan_relaxation(criteria, radius)
            if relaxed_criteria is not None:
                relaxed_resolved = _resolve_anchors(relaxed_criteria)
                if run_pass(label, relaxed_criteria, relaxed_resolved, new_radius):
                    relaxed.append(label)

        if len(raw_profiles) < config.TARGET_MERGED_POOL_SIZE:
            broad = _broad_retrieval_criteria(criteria)
            broad_resolved = _resolve_anchors(broad)
            if run_pass("broadened retrieval", broad, broad_resolved, config.GEO_RADIUS_BROAD_MILES):
                relaxed.append("broadened retrieval")
    except CrustdataError as exc:
        return jsonify({"error": str(exc)}), exc.status

    candidates = pool.compress(raw_profiles)
    ranked = ranker.rank(candidates, criteria, hiring_company_id=resolved.hiring_company_id,
                         anchor_company_ids=resolved.anchor_company_ids)

    result = {
        "from_cache": False,
        "candidates": ranked,
        "total_count": max((p["total_count"] for p in search_passes), default=0),
        "returned": len(ranked),
        "search_passes": search_passes,
        "relaxed": relaxed,
        "criteria": criteria.to_dict(),
    }
    put_cached(cache_key, {"criteria": criteria.to_dict(), "limit": limit}, result, _summarize(criteria))
    return jsonify(result)
