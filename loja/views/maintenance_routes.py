"""Boundaries internos para manutenção agendada.

A rota de cron é fail-closed: exige CRON_SECRET válido e a flag
STOCK_RESERVATION_RELEASE_ENABLED. Não consulta Mercado Pago, não cancela
pedidos e não chama fornecedores.
"""

import hmac
import os

from flask import Blueprint, jsonify, request

from loja import csrf
from loja.services.stock_reservations import (
    is_scheduled_release_enabled,
    release_expired_stock_reservations,
)

maintenance_blueprint = Blueprint("maintenance", __name__)


def _cron_authorized():
    expected = str(os.environ.get("CRON_SECRET") or "").strip()
    authorization = str(request.headers.get("Authorization") or "")
    supplied = (
        authorization[len("Bearer "):].strip()
        if authorization.startswith("Bearer ")
        else ""
    )
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


@maintenance_blueprint.route(
    "/internal/maintenance/release-expired-reservations",
    methods=["GET"],
)
@csrf.exempt
def release_expired_reservations_cron():
    if not _cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if not is_scheduled_release_enabled():
        return jsonify({"ok": True, "enabled": False, "released": 0}), 200

    try:
        result = release_expired_stock_reservations(source="cron")
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_configuration"}), 503

    return jsonify({
        "ok": True,
        "enabled": True,
        "candidates": result.candidates,
        "released": result.released,
        "skipped": result.skipped,
        "hours": result.hours,
    }), 200
