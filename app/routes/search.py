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

from flask import Blueprint, jsonify, request

from .. import config
from ..core import crustdata, filters as filters_mod, pool, ranker
from ..core.criteria import Criteria
from ..core.crustdata import CrustdataError
from ..core.filters import Resolved, build_filters
from ..db import cache_key_for, get_cached, put_cached

bp = Blueprint("search", __name__)


def _resolve_anchors(criteria: Criteria) -> Resolved:
    """Resolve everything build_filters can't compute itself: company names ->
    ids (identify, cached) and industry/school -> enum values (autocomplete).
    All fail-soft, so an unresolvable name just drops that clause."""
    company_ids = [cid for c in criteria.anchor_companies if (cid := crustdata.identify(c))]
    exclude_ids = [cid for c in criteria.exclude_employers if (cid := crustdata.identify(c))]
    hiring_id = crustdata.identify(criteria.hiring_company) if criteria.hiring_company.strip() else None

    industries: list[str] = []
    for ind in criteria.anchor_industries:
        industries += crustdata.autocomplete("linkedin_industries", ind)
    schools: list[str] = []
    for sch in criteria.education.schools:
        schools += crustdata.autocomplete("education_background.institute_name", sch)

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
    criteria = Criteria.from_dict(request.get_json(force=True, silent=True) or {})
    if criteria.is_empty():
        return jsonify({"error": "Add at least one criterion."}), 400

    resolved = _resolve_anchors(criteria)
    radius = config.GEO_RADIUS_DEFAULT_MILES
    payload = build_filters(criteria, resolved, geo_radius_miles=radius)
    if not payload["conditions"]:
        return jsonify({"error": "Add at least one criterion."}), 400

    limit = int((request.get_json(force=True, silent=True) or {}).get("limit", config.SEARCH_LIMIT))
    cache_key = cache_key_for({"filters": payload, "limit": limit})
    cached = get_cached(cache_key)
    if cached is not None:
        return jsonify({**cached, "from_cache": True})

    try:
        data = crustdata.search(payload, limit=limit)
        relaxed: list[str] = []
        # One relaxation pass if the pool is thin. Rank against ORIGINAL criteria.
        if (data.get("total_count") or 0) < config.BROAD_HEALTHY_TOTAL_COUNT:
            relaxed_criteria, new_radius, label = ranker.plan_relaxation(criteria, radius)
            if relaxed_criteria is not None:
                relaxed_resolved = _resolve_anchors(relaxed_criteria)
                relaxed_payload = build_filters(relaxed_criteria, relaxed_resolved,
                                                geo_radius_miles=new_radius)
                if relaxed_payload["conditions"]:
                    data = crustdata.search(relaxed_payload, limit=limit)
                    relaxed.append(label)
    except CrustdataError as exc:
        return jsonify({"error": str(exc)}), exc.status

    candidates = pool.compress(data.get("profiles") or [])
    ranked = ranker.rank(candidates, criteria, hiring_company_id=resolved.hiring_company_id)

    result = {
        "from_cache": False,
        "candidates": ranked,
        "total_count": data.get("total_count") or 0,
        "returned": len(ranked),
        "relaxed": relaxed,
        "criteria": criteria.to_dict(),
    }
    put_cached(cache_key, {"filters": payload, "limit": limit}, result, _summarize(criteria))
    return jsonify(result)
