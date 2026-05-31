"""Intake endpoints.

POST /api/extract  (multipart `file`) | {url}  -> {text, source}
    Server-side JD ingestion: parse an uploaded PDF/DOCX/TXT, or fetch + strip a
    job-posting URL. Returned text is fed back into /api/chat as jd_text.

POST /api/chat  {messages: [...], jd_text?, url?}  -> {reply, criteria, ready_to_search}
    One conversational intake turn. Powers the hybrid flow: the form's "Search
    Candidates" sends the describe box / JD / notes as a single message; if the
    model signals ready_to_search, the UI searches immediately, otherwise it
    shows the follow-up question.

The Claude turn (intake.run_turn) needs ANTHROPIC_API_KEY; until it's set this
returns 503 and the UI falls back to Advanced Search (manual criteria).
"""

from flask import Blueprint, jsonify, request

from ..core import cluster_finder, intake, jd_fetch
from ..core.intake import IntakeError
from ..llm import LLMUnavailable

bp = Blueprint("chat", __name__)


@bp.route("/api/extract", methods=["POST"])
def extract():
    if "file" in request.files:
        try:
            text, source = jd_fetch.read_uploaded_text(request.files["file"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not text.strip():
            return jsonify({"error": "Couldn't read any text from that file."}), 400
        return jsonify({"text": text, "source": source})

    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if url:
        try:
            text = jd_fetch.fetch_from_url(url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"text": text, "source": "url"})

    return jsonify({"error": "Provide a file or a url."}), 400


@bp.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True, silent=True) or {}
    messages = body.get("messages") or []

    jd_text = (body.get("jd_text") or "").strip()
    url = (body.get("url") or "").strip()
    if url and not jd_text:
        try:
            jd_text = jd_fetch.fetch_from_url(url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    try:
        reply, criteria, ready = intake.run_turn(messages, jd_text=jd_text)
    except LLMUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except IntakeError as exc:
        return jsonify({"error": str(exc)}), exc.status

    # Dedicated cluster step: when intake wants a company cluster but hasn't been
    # handed explicit names, build the best peer set with the stronger model.
    wants_cluster = bool(criteria.cluster_hint) or (
        criteria.anchor_strategy in ("companies", "both") and not criteria.anchor_companies
    )
    if wants_cluster:
        companies = cluster_finder.find_cluster(criteria)  # fail-soft -> []
        if companies:
            have = {c.lower() for c in criteria.anchor_companies}
            criteria.anchor_companies += [c for c in companies if c.lower() not in have]
            if criteria.anchor_strategy == "none":
                criteria.anchor_strategy = "companies"

    return jsonify({
        "reply": reply,
        "criteria": criteria.to_dict(),
        "ready_to_search": ready,
        "jd_chars": len(jd_text),
    })
