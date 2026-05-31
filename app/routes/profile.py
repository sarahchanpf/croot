"""Opt-in contact enrichment.

GET /api/profile?linkedin_url=...  -> {profiles: [enriched profile]}

This is the expensive Crustdata call (~4 cr/profile), so it's gated behind an
explicit user action and cached 30 days (per-URL, inside crustdata.enrich).
"""

from flask import Blueprint, jsonify, request

from ..core import crustdata
from ..core.crustdata import CrustdataError

bp = Blueprint("profile", __name__)


@bp.route("/api/profile")
def profile():
    url = (request.args.get("linkedin_url") or "").strip()
    if not url:
        return jsonify({"error": "linkedin_url is required."}), 400
    try:
        data = crustdata.enrich([url], include_contact=True)
    except CrustdataError as exc:
        return jsonify({"error": str(exc)}), exc.status
    if not data["profiles"]:
        return jsonify({"error": "Profile not found."}), 404
    return jsonify(data)
