"""Alpha access gate.

POST /api/access with {password} validates the alpha password.
POST /api/access with {password, name, email} stores the visitor record.
"""

import re
import time
from contextlib import closing

import requests
from flask import Blueprint, jsonify, request, session

from ..config import ACCESS_PASSWORD, FREE_SEARCH_LIMIT, SIGNUP_WEBHOOK_URL
from ..db import add_to_waitlist, db, get_search_count, is_waitlisted

bp = Blueprint("access", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _notify_signup(event: str, name: str, email: str) -> None:
    """Best-effort: mirror a signup to the durable webhook (a Google Apps Script
    web app that appends to a Sheet) so names/emails survive Vercel's ephemeral
    /tmp. Never raises — a webhook hiccup must not break the signup."""
    if not SIGNUP_WEBHOOK_URL:
        return
    try:
        requests.post(SIGNUP_WEBHOOK_URL, json={
            "event": event,
            "name": name,
            "email": email,
            "ts": int(time.time()),
            "user_agent": (request.headers.get("User-Agent") or "")[:300],
        }, timeout=5)
    except Exception:
        pass


def _usage_payload(searches_used: int) -> dict:
    used = max(0, int(searches_used))
    return {
        "search_limit": FREE_SEARCH_LIMIT,
        "searches_used": used,
        "searches_remaining": max(0, FREE_SEARCH_LIMIT - used),
        "limit_reached": used >= FREE_SEARCH_LIMIT,
    }


@bp.route("/api/access/status", methods=["GET"])
def status():
    user = session.get("access_user")
    if not user or not user.get("email"):
        return jsonify({"authenticated": False}), 401

    persisted = get_search_count(user["email"])
    used = max(int(session.get("searches_used", 0)), persisted)
    session["searches_used"] = used
    joined_waitlist = bool(session.get("joined_waitlist")) or is_waitlisted(user["email"])
    session["joined_waitlist"] = joined_waitlist
    return jsonify({
        "authenticated": True,
        "user": user,
        "joined_waitlist": joined_waitlist,
        **_usage_payload(used),
    })


@bp.route("/api/access", methods=["POST"])
def access():
    body = request.get_json(force=True, silent=True) or {}
    password = body.get("password") or ""
    if password != ACCESS_PASSWORD:
        return jsonify({"error": "Incorrect password."}), 401

    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
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

    _notify_signup("access", name, email)

    previous_user = session.get("access_user") or {}
    session_count = int(session.get("searches_used", 0)) if previous_user.get("email") == email else 0
    searches_used = max(session_count, get_search_count(email))
    session.permanent = True
    session["access_user"] = {"name": name, "email": email}
    session["searches_used"] = searches_used
    session["joined_waitlist"] = (
        bool(session.get("joined_waitlist")) if previous_user.get("email") == email else False
    ) or is_waitlisted(email)

    return jsonify({
        "ok": True,
        "joined_waitlist": session["joined_waitlist"],
        **_usage_payload(searches_used),
    })


@bp.route("/api/access/waitlist", methods=["POST"])
def waitlist():
    user = session.get("access_user")
    if not user or not user.get("email"):
        return jsonify({"error": "Sign in again to join the waitlist."}), 401
    try:
        add_to_waitlist(user.get("name") or "", user["email"])
    except Exception as exc:
        return jsonify({"error": f"Could not join the waitlist: {exc}"}), 500
    _notify_signup("waitlist", user.get("name") or "", user["email"])
    session["joined_waitlist"] = True
    return jsonify({"ok": True, "joined_waitlist": True})
