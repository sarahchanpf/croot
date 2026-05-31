"""Conversational intake endpoint.

POST /api/chat  {messages: [...], url?: str}
  -> {reply, criteria, ready_to_search}

If `url` is present it's fetched + stripped to JD text first (real, working).
The Claude turn (intake.run_turn) is stubbed until ANTHROPIC_API_KEY is wired,
so this returns 501 with a clear message in the meantime.

TODO(impl): stream the reply over SSE once intake is live.
"""

from flask import Blueprint, jsonify, request

from ..core import intake, jd_fetch
from ..llm import LLMUnavailable

bp = Blueprint("chat", __name__)


@bp.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True, silent=True) or {}
    messages = body.get("messages") or []

    jd_text = ""
    url = (body.get("url") or "").strip()
    if url:
        try:
            jd_text = jd_fetch.fetch_from_url(url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    try:
        reply, criteria, ready = intake.run_turn(messages, jd_text=jd_text)
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except NotImplementedError:
        return jsonify({"error": "Intake not implemented yet."}), 501

    return jsonify({
        "reply": reply,
        "criteria": criteria.to_dict(),
        "ready_to_search": ready,
        "jd_chars": len(jd_text),
    })
