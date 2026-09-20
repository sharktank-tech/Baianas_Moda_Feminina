"""Restricted backend API; no supplier operations or financial authorization."""

import os
from flask import Blueprint, abort, jsonify, request
from flask_login import current_user

from loja.modules.canary_evidence_models import CanaryEvidenceCase, CanaryEvidenceEvent
from loja.services.canary_evidence import EvidenceRejected, open_case, permitted, record


canary_evidence_blueprint = Blueprint("canary_evidence", __name__, url_prefix="/admin/canary-evidence")


def _error(exc):
    code = str(exc)
    return jsonify({"error": code}), (403 if code == "access_denied" else 409)


@canary_evidence_blueprint.before_request
def _require_explicit_installation():
    # No queries to new tables until a controlled schema installation and
    # server-side role assignment have both been reviewed.
    if os.environ.get("CANARY_EVIDENCE_REGISTER_ENABLED") != "1":
        abort(404)


@canary_evidence_blueprint.post("/cases")
def create_case():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_event"}), 400
    try:
        case = open_case(data.get("order_id"), current_user, data.get("purpose", "simulation"))
    except EvidenceRejected as exc:
        return _error(exc)
    return jsonify({"case_id": str(case.id), "state": case.state}), 201


@canary_evidence_blueprint.post("/cases/events")
def add_event():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_event"}), 400
    try:
        state = record(data.get("case_id"), current_user, data.get("kind"),
                       data.get("result"), data.get("evidence_ref"))
    except EvidenceRejected as exc:
        return _error(exc)
    return jsonify({"state": state}), 201


@canary_evidence_blueprint.get("/cases/<uuid:case_id>")
def read_case(case_id):
    if not any(permitted(current_user, role) for role in ("technical", "finance", "support", "reviewer")):
        abort(403)
    case = CanaryEvidenceCase.query.filter_by(id=case_id).first_or_404()
    events = (CanaryEvidenceEvent.query.filter_by(case_id=case.id)
              .order_by(CanaryEvidenceEvent.created_at, CanaryEvidenceEvent.id).all())
    return jsonify({"state": case.state,
                    "events": [{"kind": e.kind, "result": e.result,
                                "created_at": e.created_at.isoformat()} for e in events]})
