"""Search endpoints.

POST /api/preview  {criteria}  -> {total_count}    (limit:1, cheap pre-spend check)
POST /api/search   {criteria}  -> {candidates, relaxed, total_count}

Both build filters from the Criteria contract, resolve company anchors, call
Crustdata, compress + rank. Core is stubbed, so these return 501 until the
filter/crustdata/ranker modules are implemented.
"""

from flask import Blueprint, jsonify, request

from ..core import filters, ranker
from ..core.criteria import Criteria

bp = Blueprint("search", __name__)


@bp.route("/api/preview", methods=["POST"])
def preview():
    criteria = Criteria.from_dict(request.get_json(force=True, silent=True) or {})
    if criteria.is_empty():
        return jsonify({"error": "Add at least one criterion."}), 400
    try:
        _ = filters.build_filters(criteria)
    except NotImplementedError:
        return jsonify({"error": "Search not implemented yet."}), 501
    # TODO(impl): crustdata.search(limit=1) -> total_count
    return jsonify({"error": "Search not implemented yet."}), 501


@bp.route("/api/search", methods=["POST"])
def search():
    criteria = Criteria.from_dict(request.get_json(force=True, silent=True) or {})
    if criteria.is_empty():
        return jsonify({"error": "Add at least one criterion."}), 400
    try:
        _ = filters.build_filters(criteria)
        # TODO(impl): identify anchors -> crustdata.search (full-fat) ->
        # pool.compress -> ranker.rank -> relaxation pass if thin -> cache.
        _ = ranker
    except NotImplementedError:
        return jsonify({"error": "Search not implemented yet."}), 501
    return jsonify({"error": "Search not implemented yet."}), 501
