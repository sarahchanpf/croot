"""Opt-in contact enrichment.

GET /api/profile?linkedin_url=...  -> enriched profile (personal email/phone)

This is the expensive Crustdata call (~4 cr/profile), so it's gated behind an
explicit user action and cached 30 days. Stubbed until crustdata.enrich lands.
"""

from flask import Blueprint, jsonify, request

from ..core import crustdata

bp = Blueprint("profile", __name__)


@bp.route("/api/profile")
def profile():
    url = (request.args.get("linkedin_url") or "").strip()
    if not url:
        return jsonify({"error": "linkedin_url is required."}), 400
    try:
        data = crustdata.enrich([url], include_contact=True)
    except NotImplementedError:
        return jsonify({"error": "Enrichment not implemented yet."}), 501
    return jsonify(data)
