"""Fila administrativa de fulfillment AliExpress (Fase 8.0).

GET  /admin/integrations/aliexpress/pedidos              -> lista de fulfillments
GET  /admin/integrations/aliexpress/pedidos/<uuid>       -> detalhe do fulfillment
POST /admin/integrations/aliexpress/pedidos/<uuid>/revalidar -> revalida local/credencial

Segurança:
  * Todas as rotas exigem @admin_required + CSRF
  * Nunca expõe: access_token, refresh_token, ciphertext, supplier_sku_id,
    supplier_product_external_id, raw provider request
  * Logs apenas contagens/IDs locais
"""

from __future__ import annotations

import os
import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    render_template_string,
    request,
    url_for,
)
from sqlalchemy import desc, func
from sqlalchemy.orm import selectinload

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressOrderItem, Order, Supplier
from loja.integrations.aliexpress import (
    ensure_aliexpress_fulfillment,
    get_fulfillment_access_token,
    sync_aliexpress_order_status,
    CredentialNotConfigured,
    CredentialNotFound,
    CredentialExpired,
    CredentialInvalid,
)
from loja.integrations.aliexpress.client import AliExpressClient
from loja.integrations.aliexpress.credential_store import AliExpressCredential
from loja.integrations.aliexpress.ds_order_status import (
    DS_ORDER_STATUS_METHOD,
    AliExpressDSOrderStatusSyncer,
    map_aliexpress_ds_status,
)
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusSyncError,
)
from loja.utils.decorators import admin_required

fulfillment_admin_blueprint = Blueprint(
    "aliexpress_fulfillment_admin",
    __name__,
    url_prefix="/admin/integrations/aliexpress/pedidos",
)


@fulfillment_admin_blueprint.route("/", methods=["GET"], strict_slashes=False)
@admin_required
def index():
    """Lista de fulfillments AliExpress com paginação."""
    page = request.args.get("page", 1, type=int)
    per_page = 20

    query = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order).selectinload(Order.user),
    ).order_by(desc(AliExpressOrder.updated_at))

    state_filter = request.args.get("state")
    if state_filter:
        query = query.filter(AliExpressOrder.state == state_filter)

    supplier_filter = request.args.get("supplier_id")
    if supplier_filter:
        query = query.filter(AliExpressOrder.supplier_id == supplier_filter)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    suppliers = Supplier.query.filter_by(channel_type="aliexpress").all()
    states = [
        "queued", "blocked", "ready", "submitting", "submitted",
        "pending_reconciliation",
        "confirmed", "shipped", "delivered", "cancel_requested",
        "cancelled", "failed", "manual_review", "pending"
    ]

    return render_template(
        "admin/aliexpress/fulfillment_list.html",
        pagination=pagination,
        suppliers=suppliers,
        states=states,
        current_state=state_filter,
        current_supplier=supplier_filter,
    )


@fulfillment_admin_blueprint.route("/<uuid:order_id>", methods=["GET"])
@admin_required
def detail(order_id):
    """Detalhe de um fulfillment AliExpress."""
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.product),
        selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.variant),
        selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.order_item),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order).selectinload(Order.user),
    ).filter_by(id=order_id).first_or_404()

    return render_template(
        "admin/aliexpress/fulfillment_detail.html",
        ali_order=ali_order,
    )


@fulfillment_admin_blueprint.route("/<uuid:order_id>/revalidar", methods=["POST"])
@admin_required
def revalidate(order_id):
    """Revalida fulfillment: roda validações locais + credencial novamente."""
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order),
    ).filter_by(id=order_id).first_or_404()

    if ali_order.state not in ("queued", "blocked", "ready"):
        flash(
            f"Fulfillment em estado {ali_order.state} não pode ser revalidado.",
            "warning",
        )
        return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))

    if not ali_order.order:
        flash("Pedido original não encontrado.", "danger")
        return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))

    try:
        ensure_aliexpress_fulfillment(ali_order.order)
        db.session.commit()
        db.session.refresh(ali_order)

        if ali_order.state == "ready":
            flash("Fulfillment revalidado com sucesso — estado: READY.", "success")
        else:
            flash(
                f"Fulfillment revalidado — estado: {ali_order.state.upper()} "
                f"({ali_order.last_error}).",
                "warning",
            )

    except CredentialNotConfigured:
        db.session.rollback()
        flash("Credencial não configurada (ALIEXPRESS_CREDENTIAL_KEY ausente).", "danger")
    except CredentialNotFound:
        db.session.rollback()
        flash("Credencial não encontrada para o supplier.", "danger")
    except CredentialExpired:
        db.session.rollback()
        flash("Access token expirado (sem auto-refresh nesta fase).", "danger")
    except CredentialInvalid as exc:
        db.session.rollback()
        flash(f"Credencial inválida: {exc}", "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Erro ao revalidar fulfillment %s", order_id)
        flash("Erro interno ao revalidar fulfillment.", "danger")

    return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))


@fulfillment_admin_blueprint.route("/<uuid:order_id>/sincronizar-status", methods=["POST"])
@admin_required
def sync_status(order_id):
    """Sincroniza status do fulfillment (Fase 8.0.6) — HARD-OFF."""
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order),
    ).filter_by(id=order_id).first_or_404()

    eligible = (
        ali_order.ali_order_id
        and ali_order.state
        in ("submitted", "confirmed", "shipped", "delivered")
    )
    if not eligible:
        flash(
            f"Fulfillment em estado {ali_order.state} não pode ter status "
            "sincronizado.",
            "warning",
        )
        return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))

    try:
        result = sync_aliexpress_order_status(ali_order)
        db.session.commit()
        state = (result.state if hasattr(result, "state") else ali_order.state) or ali_order.state
        flash(f"Status sincronizado — estado: {state.upper()}.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.error("Erro ao sincronizar status %s (sanitizado)", order_id)
        flash("Erro interno ao sincronizar status.", "danger")

    return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))


# --------------------------------------------------------------------------
# Fase 8.0.7B — probes remote-only, temporários, Preview-only
# --------------------------------------------------------------------------
_BUYER_ORDER_STATUS_METHOD = "aliexpress.trade.ds.order.get"
_REMOTE_PROBE_FORM = """
<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>AliExpress DS status probe</title></head>
<body style="font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px">
<h1>AliExpress order status probes</h1>
<p>Preview only · read-only · uma chamada por clique · nenhum pedido local será criado.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Número de referência / Order ID remoto</label><br>
<input name="remote_order_id" autocomplete="off" inputmode="numeric" required
style="width:100%;padding:10px;margin:8px 0 16px">
<div style="display:flex;gap:12px;flex-wrap:wrap">
<button type="submit" name="probe_kind" value="ds_trade">Testar ds.trade.order.get</button>
<button type="submit" name="probe_kind" value="trade_ds">Testar buyer-order trade.ds.order.get</button>
</div>
</form>
<p style="margin-top:24px">Os botões são diagnósticos independentes. Não existe fallback automático no adapter canônico.</p>
</body></html>
"""

_REMOTE_PROBE_RESULT = """
<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>AliExpress DS status probe</title></head>
<body style="font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px">
<h1>Resultado sanitizado</h1>
<p><strong>Status:</strong> {{ status }}</p>
<p><strong>Método:</strong> {{ method }}</p>
{% if outcome %}<p><strong>Outcome:</strong> {{ outcome }}</p>{% endif %}
{% if request_id %}<p><strong>Request ID:</strong> {{ request_id }}</p>{% endif %}
{% if error %}<p><strong>Erro:</strong> {{ error }}</p>{% endif %}
<p>O número do pedido não foi persistido nem exibido nesta resposta.</p>
<p><a href="{{ url_for('aliexpress_fulfillment_admin.remote_status_probe') }}">Voltar</a></p>
</body></html>
"""


def _assert_remote_probe_preview():
    if os.environ.get("VERCEL_ENV") != "preview":
        abort(404)
    if os.environ.get("VERCEL_GIT_COMMIT_REF") != "feature/aliexpress-fulfillment-core":
        abort(404)


def _single_active_aliexpress_supplier_id():
    rows = AliExpressCredential.query.filter_by(status="active").all()
    eligible = [
        row for row in rows
        if row.supplier is not None
        and row.supplier.channel_type == "aliexpress"
        and bool(row.supplier.seller_id)
    ]
    return eligible[0].supplier_id if len(eligible) == 1 else None


def _sanitized_error_code(exc) -> str:
    code = getattr(exc, "code", None)
    if code is None:
        return "unknown"
    text = str(code).strip()
    if 0 < len(text) <= 32 and all(c.isalnum() or c in "_.-" for c in text):
        return text
    return "invalid"


def _run_buyer_order_diagnostic(supplier_id, remote_order_id):
    """Uma chamada explícita ao buyer-order API; nunca é fallback runtime."""
    app_key = os.environ.get("ALIEXPRESS_APP_KEY")
    app_secret = os.environ.get("ALIEXPRESS_APP_SECRET")
    if not app_key or not app_secret:
        raise AliExpressOrderStatusSyncError(
            "ALIEXPRESS_APP_KEY/ALIEXPRESS_APP_SECRET ausentes para diagnóstico"
        )

    try:
        token = get_fulfillment_access_token(supplier_id)
    except (CredentialNotConfigured, CredentialNotFound, CredentialExpired, CredentialInvalid):
        raise AliExpressOrderStatusSyncError(
            "credencial AliExpress indisponível para diagnóstico"
        ) from None

    client = AliExpressClient(
        app_key=app_key,
        app_secret=app_secret,
        access_token=token,
        timeout=10.0,
    )
    try:
        payload = client.call(
            _BUYER_ORDER_STATUS_METHOD,
            single_order_query={"order_id": remote_order_id},
        )
    except AliExpressAuthError as exc:
        raise AliExpressOrderStatusSyncError(
            f"erro de autenticação buyer-order (code={_sanitized_error_code(exc)})"
        ) from None
    except AliExpressRateLimitError as exc:
        raise AliExpressOrderStatusSyncError(
            f"rate limit buyer-order (code={_sanitized_error_code(exc)})"
        ) from None
    except AliExpressAPIError as exc:
        raise AliExpressOrderStatusSyncError(
            f"erro de API buyer-order (code={_sanitized_error_code(exc)})"
        ) from None
    except AliExpressTransportError:
        raise AliExpressOrderStatusSyncError(
            "falha de transporte buyer-order"
        ) from None
    except Exception:
        raise AliExpressOrderStatusSyncError(
            "erro inesperado buyer-order"
        ) from None

    if not isinstance(payload, dict):
        raise AliExpressOrderStatusSyncError("payload buyer-order inesperado")
    data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    order_status = str(data.get("order_status") or "").strip()
    logistics_status = str(data.get("logistics_status") or "").strip()
    if not (order_status or logistics_status):
        raise AliExpressOrderStatusSyncError("payload buyer-order sem status")
    return map_aliexpress_ds_status(order_status, logistics_status)


@fulfillment_admin_blueprint.route(
    "/remote-status-probe", methods=["GET", "POST"], strict_slashes=False
)
@admin_required
def remote_status_probe():
    """Uma consulta real por clique, remote-only, sem persistência, Preview-only."""
    _assert_remote_probe_preview()

    if request.method == "GET":
        return render_template_string(_REMOTE_PROBE_FORM)

    remote_order_id = (request.form.get("remote_order_id") or "").strip()
    probe_kind = (request.form.get("probe_kind") or "ds_trade").strip()
    supplier_id = _single_active_aliexpress_supplier_id()
    if supplier_id is None:
        db.session.rollback()
        return render_template_string(
            _REMOTE_PROBE_RESULT,
            status="failure", method="n/a",
            outcome=None, request_id=None,
            error="credencial/supplier AliExpress ativo não é único",
        ), 409

    if probe_kind == "trade_ds":
        try:
            outcome = _run_buyer_order_diagnostic(supplier_id, remote_order_id)
        except AliExpressOrderStatusSyncError as exc:
            db.session.rollback()
            return render_template_string(
                _REMOTE_PROBE_RESULT,
                status="failure", method=_BUYER_ORDER_STATUS_METHOD,
                outcome=None, request_id=None, error=str(exc),
            ), 502
        except Exception:
            db.session.rollback()
            return render_template_string(
                _REMOTE_PROBE_RESULT,
                status="failure", method=_BUYER_ORDER_STATUS_METHOD,
                outcome=None, request_id=None, error="erro interno sanitizado",
            ), 500

        db.session.rollback()
        return render_template_string(
            _REMOTE_PROBE_RESULT,
            status="success", method=_BUYER_ORDER_STATUS_METHOD,
            outcome=outcome, request_id=None, error=None,
        )

    sentinel = uuid.UUID(int=0)
    status_request = AliExpressOrderStatusRequest(
        aliexpress_order_id=sentinel,
        order_id=sentinel,
        supplier_id=supplier_id,
        ali_order_id=remote_order_id,
        attempt_key=None,
    )
    syncer = AliExpressDSOrderStatusSyncer()

    try:
        result = syncer.sync(status_request)
    except AliExpressOrderStatusSyncError as exc:
        db.session.rollback()
        return render_template_string(
            _REMOTE_PROBE_RESULT,
            status="failure", method=DS_ORDER_STATUS_METHOD,
            outcome=None, request_id=None, error=str(exc),
        ), 502
    except Exception:
        db.session.rollback()
        return render_template_string(
            _REMOTE_PROBE_RESULT,
            status="failure", method=DS_ORDER_STATUS_METHOD,
            outcome=None, request_id=None, error="erro interno sanitizado",
        ), 500

    db.session.rollback()
    return render_template_string(
        _REMOTE_PROBE_RESULT,
        status="success", method=DS_ORDER_STATUS_METHOD,
        outcome=result.outcome, request_id=syncer.last_request_id, error=None,
    )
