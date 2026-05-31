"""Search history + saved searches (named criteria a recruiter can re-run)."""

import json
import time
from contextlib import closing

from flask import Blueprint, jsonify, request

from ..config import CACHE_TTL_SECONDS  # noqa: F401  (reserved for history TTL use)
from ..db import db

bp = Blueprint("history", __name__)


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
    if request.method == "GET":
        try:
            with closing(db()) as conn:
                rows = conn.execute(
                    "SELECT id, name, criteria, created_at, last_run_at "
                    "FROM saved_searches ORDER BY created_at DESC"
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["criteria"] = json.loads(d["criteria"])
                out.append(d)
            return jsonify(out)
        except Exception:
            return jsonify([])

    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    criteria = body.get("criteria")
    if not name or criteria is None:
        return jsonify({"error": "name and criteria are required."}), 400
    try:
        with closing(db()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO saved_searches (name, criteria, created_at) VALUES (?, ?, ?)",
                (name, json.dumps(criteria), int(time.time())),
            )
        return jsonify({"id": cur.lastrowid, "name": name}), 201
    except Exception as exc:
        return jsonify({"error": f"Could not save: {exc}"}), 500


@bp.route("/api/saved-searches/<int:search_id>", methods=["DELETE"])
def delete_saved_search(search_id: int):
    try:
        with closing(db()) as conn, conn:
            conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        return jsonify({"deleted": search_id})
    except Exception as exc:
        return jsonify({"error": f"Could not delete: {exc}"}), 500
