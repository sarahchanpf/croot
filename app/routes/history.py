"""Per-user saved searches (named criteria a recruiter can re-run).

Stored durably in the Google Sheet via the Apps Script webhook (Vercel's /tmp is
ephemeral), scoped to the signed-in user's email. The legacy /api/history reads
the local search_history table (best-effort; empty on Vercel's read-only fs).
"""

import json
from contextlib import closing

from flask import Blueprint, jsonify, request, session

from ..db import db
from ..notify import get_webhook, request_webhook

bp = Blueprint("history", __name__)


def _user_email() -> str:
    user = session.get("access_user") or {}
    return (user.get("email") or "").strip().lower()


@bp.route("/api/history")
def history():
    try:
        with closing(db()) as conn:
            rows = conn.execute(
                "SELECT summary, created_at FROM search_history "
                "ORDER BY created_at DESC LIMIT 25"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])  # cache unavailable (read-only fs)


@bp.route("/api/saved-searches", methods=["GET", "POST"])
def saved_searches():
    email = _user_email()
    if not email:
        return jsonify({"error": "Sign in to use saved searches."}), 401

    if request.method == "GET":
        data = get_webhook({"action": "list_saved", "email": email})
        items = data if isinstance(data, list) else []
        # criteria comes back as a JSON string from the Sheet — parse for the UI.
        for it in items:
            if isinstance(it.get("criteria"), str):
                try:
                    it["criteria"] = json.loads(it["criteria"])
                except (ValueError, TypeError):
                    it["criteria"] = {}
        return jsonify(items)

    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    criteria = body.get("criteria")
    if not name or criteria is None:
        return jsonify({"error": "name and criteria are required."}), 400
    resp = request_webhook({
        "event": "save_search",
        "email": email,
        "name": name[:200],
        "query": (body.get("query") or "")[:500],
        "criteria": json.dumps(criteria),
    })
    if not resp or not resp.get("ok"):
        return jsonify({"error": "Couldn't save the search — storage is unavailable."}), 502
    return jsonify({"id": resp.get("id"), "name": name}), 201


@bp.route("/api/saved-searches/<sid>", methods=["DELETE"])
def delete_saved_search(sid: str):
    email = _user_email()
    if not email:
        return jsonify({"error": "Sign in to use saved searches."}), 401
    resp = request_webhook({"event": "delete_saved", "email": email, "id": sid})
    if not resp or not resp.get("ok"):
        return jsonify({"error": "Couldn't delete the saved search."}), 502
    return jsonify({"deleted": sid})
