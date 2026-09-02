"""Rota administrativa temporária para o probe remote-only 8.0.7B.

Somente Preview da branch feature/aliexpress-fulfillment-core.
Recebe o remote_order_id via POST (nunca em URL), exige admin + CSRF,
usa a única credencial AliExpress ativa e faz UMA consulta read-only por
aliexpress.ds.trade.order.get. Nunca persiste o order id nem cria AliExpressOrder.
"""

from __future__ import annotations

import os
import uuid

from flask import Blueprint, abort, render_template_string, request

from loja import db
from loja.integrations.aliexpress.credential_store import AliExpressCredential
from loja.integrations.aliexpress.ds_order_status import (
    DS_ORDER_STATUS_METHOD,
    AliExpressDSOrderStatusSyncer,
)
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusSyncError,
)
from loja.utils.decorators import admin_required

remote_probe_admin_blueprint = Blueprint(
    "aliexpress_remote_probe_admin",
    __name__,
    url_prefix="/admin/integrations/aliexpress/remote-status-probe",
)

_EXPECTED_BRANCH = "feature/aliexpress-fulfillment-core"


def _preview_only() -> None:
    if os.environ.get("VERCEL_ENV") != "preview":
        abort(404)
    if os.environ.get("VERCEL_GIT_COMMIT_REF") != _EXPECTED_BRANCH:
        abort(404)


def _single_active_supplier_id():
    rows = (
        AliExpressCredential.query
        .filter_by(status="active")
        .all()
    )
    eligible = [
        row for row in rows
        if row.supplier is not None
        and row.supplier.channel_type == "aliexpress"
        and bool(row.supplier.seller_id)
    ]
    if len(eligible) != 1:
        return None
    return eligible[0].supplier_id


_FORM = """
<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>AliExpress DS status probe</title></head>
<body style="font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px">
  <h1>AliExpress DS status probe</h1>
  <p>Preview only · read-only · exatamente uma chamada · nenhum pedido local será criado.</p>
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Número de referência / Order ID remoto</label><br>
    <input name="remote_order_id" autocomplete="off" inputmode="numeric" required
           style="width:100%;padding:10px;margin:8px 0 16px">
    <button type="submit">Executar probe read-only</button>
  </form>
</body>
</html>
"""


_RESULT = """
<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>AliExpress DS status probe</title></head>
<body style="font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px">
  <h1>Resultado sanitizado</h1>
  <p><strong>Status:</strong> {{ status }}</p>
  <p><strong>Método:</strong> {{ method }}</p>
  {% if outcome %}<p><strong>Outcome:</strong> {{ outcome }}</p>{% endif %}
  {% if request_id %}<p><strong>Request ID:</strong> {{ request_id }}</p>{% endif %}
  {% if error %}<p><strong>Erro:</strong> {{ error }}</p>{% endif %}
  <p>O número do pedido não foi persistido nem exibido nesta resposta.</p>
</body>
</html>
"""


@remote_probe_admin_blueprint.route("/", methods=["GET", "POST"], strict_slashes=False)
@admin_required
def index():
    _preview_only()

    if request.method == "GET":
        return render_template_string(_FORM)

    remote_order_id = (request.form.get("remote_order_id") or "").strip()
    supplier_id = _single_active_supplier_id()
    if supplier_id is None:
        db.session.rollback()
        return render_template_string(
            _RESULT,
            status="failure",
            method=DS_ORDER_STATUS_METHOD,
            outcome=None,
            request_id=None,
            error="credencial/supplier AliExpress ativo não é único",
        ), 409

    sentinel = uuid.UUID(int=0)
    req = AliExpressOrderStatusRequest(
        aliexpress_order_id=sentinel,
        order_id=sentinel,
        supplier_id=supplier_id,
        ali_order_id=remote_order_id,
        attempt_key=None,
    )
    syncer = AliExpressDSOrderStatusSyncer()

    try:
        result = syncer.sync(req)
    except AliExpressOrderStatusSyncError as exc:
        db.session.rollback()
        return render_template_string(
            _RESULT,
            status="failure",
            method=DS_ORDER_STATUS_METHOD,
            outcome=None,
            request_id=None,
            error=str(exc),
        ), 502
    except Exception:
        db.session.rollback()
        return render_template_string(
            _RESULT,
            status="failure",
            method=DS_ORDER_STATUS_METHOD,
            outcome=None,
            request_id=None,
            error="erro interno sanitizado",
        ), 500

    # Read-only absoluto: descarta até flush incidental do credential_store.
    db.session.rollback()
    return render_template_string(
        _RESULT,
        status="success",
        method=DS_ORDER_STATUS_METHOD,
        outcome=result.outcome,
        request_id=syncer.last_request_id,
        error=None,
    )


__all__ = ["remote_probe_admin_blueprint"]
