"""Export endpoint — routes a ranked candidate set through a Destination.

POST /api/export  {candidates: [...], meta: {...}, kind: "csv"}
  -> file download (CSV) for now; future kinds resolve to other destinations.
"""

from flask import Blueprint, Response, jsonify, request

from ..core.export.csv_dest import CSVDestination

bp = Blueprint("export", __name__)

# Registry of available destinations. Add "sheet"/"gem" here later.
DESTINATIONS = {
    "csv": CSVDestination,
}


@bp.route("/api/export", methods=["POST"])
def export():
    body = request.get_json(force=True, silent=True) or {}
    kind = (body.get("kind") or "csv").lower()
    candidates = body.get("candidates") or []
    meta = body.get("meta") or {}

    dest_cls = DESTINATIONS.get(kind)
    if not dest_cls:
        return jsonify({"error": f"Unknown export kind: {kind}"}), 400
    if not candidates:
        return jsonify({"error": "No candidates to export."}), 400

    result = dest_cls().write(candidates, meta)

    if result.content is not None:
        return Response(
            result.content,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{result.filename}"'
            },
        )
    return jsonify({"kind": result.kind, "url": result.url, "detail": result.detail})
