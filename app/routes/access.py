"""Alpha access gate.

POST /api/access with {password} validates the alpha password.
POST /api/access with {password, name, email} stores the visitor record.
"""

import re
import time
from contextlib import closing

from flask import Blueprint, jsonify, request

from ..config import ACCESS_PASSWORD
from ..db import db

bp = Blueprint("access", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("/api/access", methods=["POST"])
def access():
    body = request.get_json(force=True, silent=True) or {}
    password = body.get("password") or ""
    if password != ACCESS_PASSWORD:
        return jsonify({"error": "Incorrect password."}), 401

    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    if not name and not email:
        return jsonify({"ok": True, "next": "profile"})

    if not name or len(name) > 200:
        return jsonify({"error": "Name is required."}), 400
    if not email or len(email) > 320 or not _EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    user_agent = (request.headers.get("User-Agent") or "")[:500]
    try:
        with closing(db()) as conn, conn:
            conn.execute(
                "INSERT INTO access_users (name, email, user_agent, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, email, user_agent, int(time.time())),
            )
    except Exception as exc:
        return jsonify({"error": f"Could not store access details: {exc}"}), 500

    return jsonify({"ok": True})
