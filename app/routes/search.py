"""Search endpoints.

POST /api/preview  {criteria}  -> {total_count}    (limit:1, cheap pre-spend check)
POST /api/search   {criteria}  -> {candidates, relaxed, total_count, mode}

Orchestration (the heart of the product) — mirrors the skill's Phase 2:
    criteria -> resolve anchors -> build_filters -> pick sorts
             -> crustdata.search (full-fat, sorted, limit 100)
             -> if thin (total_count < 8): ONE highest-leverage relaxation pass,
                preserving sorts, and re-search
             -> pool.compress -> ranker.rank (against the ORIGINAL criteria)
             -> cache + history.

Candidates are always scored against what the recruiter actually asked for,
even when the filter was relaxed to find them — so the relaxed clauses show up
as misses in the rationale and the `relaxed` list tells the UI what we loosened.
"""

from flask import Blueprint, jsonify, request, session

from .. import config
from ..notify import post_event
from ..core import crustdata, pool, ranker, sort_picker
from ..core.criteria import Criteria
from ..core.crustdata import CrustdataError
from ..core.filters import Resolved, build_filters
from ..db import (
    cache_key_for,
    get_cached,
    get_search_count,
    increment_search_count,
    put_cached,
)

bp = Blueprint("search", __name__)


def _usage_payload(searches_used: int) -> dict:
    used = max(0, int(searches_used))
    return {
        "search_limit": config.FREE_SEARCH_LIMIT,
        "searches_used": used,
        "searches_remaining": max(0, config.FREE_SEARCH_LIMIT - used),
        "limit_reached": used >= config.FREE_SEARCH_LIMIT,
    }


def _current_user_usage():
    user = session.get("access_user")
    if not user or not user.get("email"):
        return None, 0
    persisted = get_search_count(user["email"])
    used = max(int(session.get("searches_used", 0)), persisted)
    session["searches_used"] = used
    return user, used


def _record_successful_search(user: dict, current_count: int) -> dict:
    try:
        updated = increment_search_count(user["email"], minimum_count=current_count)
    except Exception:
        updated = current_count + 1
    session["searches_used"] = updated
    return _usage_payload(updated)


def _log_search(user: dict, criteria: Criteria, result: dict) -> None:
    """Mirror a run search to the durable Sheet webhook (Searches tab). Fail-soft.
    Captures who searched, the query summary, and how many candidates came back."""
    post_event({
        "event": "search",
        "name": (user or {}).get("name", ""),
        "email": (user or {}).get("email", ""),
        "query": _summarize(criteria),
        "results": result.get("returned"),
        "total": result.get("total_count"),
        "relaxed": ", ".join(result.get("relaxed") or []),
    })


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


def _without_anchor(criteria: Criteria) -> Criteria:
    """A copy of the criteria with the company/industry cluster removed — the
    wider title + location + seniority pool for the adaptive broaden."""
    c = Criteria.from_dict(criteria.to_dict())
    c.anchor_strategy = "none"
    c.anchor_companies = []
    c.anchor_industries = []
    c.cluster_hint = ""
    return c


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
    user, searches_used = _current_user_usage()
    if user is None:
        return jsonify({"error": "Sign in again to search.", "reauthenticate": True}), 401
    if searches_used >= config.FREE_SEARCH_LIMIT:
        return jsonify({
            "error": "You have used all 5 free searches. Join the waitlist to keep in touch.",
            "waitlist_required": True,
            **_usage_payload(searches_used),
        }), 429

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
    # `sorts` makes the fetched slice deterministic + on-axis (skill Phase 2):
    # without it the DB returns an arbitrary slice of large pools. Preserved
    # through the relaxation pass — none of our relaxations drop the clause the
    # axis depends on (title base / YoE band / seniority survive).
    sorts = sort_picker.pick_sorts(criteria)
    cache_key = cache_key_for({
        "criteria": criteria.to_dict(),
        "limit": limit,
        "sorts": sorts,
        "algo": config.SEARCH_ALGO_VERSION,
    })
    cached = get_cached(cache_key)
    if cached is not None:
        usage = _record_successful_search(user, searches_used)
        _log_search(user, criteria, cached)
        return jsonify({**cached, "from_cache": True, **usage})

    relaxed: list[str] = []
    try:
        # One full-fat search. Then two adaptive steps (rank always runs against
        # the ORIGINAL criteria, so loosened clauses surface as misses).
        data = crustdata.search(payload, limit=limit, sorts=sorts)
        total = data.get("total_count") or 0

        # 1) Adaptive broaden: a company-anchored search that returns a thin pool
        #    means the cluster is too narrow for this role (e.g. 14 niche startups
        #    on a high-volume AE search). If the un-anchored title+geo+seniority
        #    pool is healthy, search THAT instead — the pedigree bonus in ranking
        #    keeps peer-company candidates on top.
        if resolved.anchor_company_ids and total < config.AUTO_BROADEN_BELOW:
            broad = _without_anchor(criteria)
            broad_payload = build_filters(broad, _resolve_anchors(broad), geo_radius_miles=radius)
            if broad_payload["conditions"]:
                broad_data = crustdata.search(broad_payload, limit=limit, sorts=sorts)
                if (broad_data.get("total_count") or 0) >= config.AUTO_BROADEN_BELOW:
                    data, total = broad_data, broad_data.get("total_count") or 0
                    relaxed.append("broadened beyond the company cluster")

        # 2) Thin-pool relaxation (skill Phase 2 Step 4) — only if still tiny.
        if total < config.BROAD_HEALTHY_TOTAL_COUNT:
            relaxed_criteria, new_radius, label = ranker.plan_relaxation(criteria, radius)
            if relaxed_criteria is not None:
                relaxed_resolved = _resolve_anchors(relaxed_criteria)
                relaxed_payload = build_filters(relaxed_criteria, relaxed_resolved,
                                                geo_radius_miles=new_radius)
                if relaxed_payload["conditions"]:
                    data = crustdata.search(relaxed_payload, limit=limit, sorts=sorts)
                    relaxed.append(label)
    except CrustdataError as exc:
        return jsonify({"error": str(exc)}), exc.status

    candidates = pool.compress(data.get("profiles") or [])
    ranked = ranker.rank(candidates, criteria, hiring_company_id=resolved.hiring_company_id,
                         anchor_company_ids=resolved.anchor_company_ids)

    result = {
        "from_cache": False,
        "candidates": ranked,
        "total_count": data.get("total_count") or 0,
        "returned": len(ranked),
        "sorts": sorts,
        "relaxed": relaxed,
        "criteria": criteria.to_dict(),
    }
    put_cached(cache_key, {"criteria": criteria.to_dict(), "limit": limit}, result, _summarize(criteria))
    usage = _record_successful_search(user, searches_used)
    _log_search(user, criteria, result)
    return jsonify({**result, **usage})
