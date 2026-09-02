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

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
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

    # Filtros opcionais
    state_filter = request.args.get("state")
    if state_filter:
        query = query.filter(AliExpressOrder.state == state_filter)

    supplier_filter = request.args.get("supplier_id")
    if supplier_filter:
        query = query.filter(AliExpressOrder.supplier_id == supplier_filter)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Suppliers para filtro
    suppliers = Supplier.query.filter_by(channel_type="aliexpress").all()

    # Estados possíveis para filtro
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
    """Revalida fulfillment: roda validações locais + credencial novamente.

    NÃO envia pedido ao AliExpress.
    CSRF obrigatório (via template; sem @csrf.exempt).
    Apenas estados revalidáveis (queued/blocked/ready) são reavaliados —
    estados avançados não regridem.
    """
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order),
    ).filter_by(id=order_id).first_or_404()

    # Fase 8.0.1: estados avançados não podem ser revalidados (sem regressão).
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
        # Re-executar validações (idempotente).
        ensure_aliexpress_fulfillment(ali_order.order)

        # Fase 8.0.2: não confiar no primeiro FulfillmentResult — após o ensure,
        # reler o estado FINAL persistido da AliExpressOrder pelo ID original e
        # commitar antes de basear o flash nele.
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
    """Sincroniza status do fulfillment (Fase 8.0.6) — HARD-OFF.

    Aciona o boundary ``sync_aliexpress_order_status`` com provider padrão
    hard-off (nenhuma chamada externa) sobre uma ordem elegível (identidade
    remota confiável: submitted/confirmed/shipped/delivered).
    CSRF obrigatório (via template; sem @csrf.exempt).
    Só flash de estado/código sanitizado; nunca exibe exception provider.
    """
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
        # Provider padrão é HARD-OFF; em testes, um pytest-injectable syncer
        # pode ser substituído via monkeypatch de get_default_order_status_syncer.
        result = sync_aliexpress_order_status(ali_order)
        db.session.commit()
        state = (result.state if hasattr(result, "state") else ali_order.state) or ali_order.state
        flash(f"Status sincronizado — estado: {state.upper()}.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.error("Erro ao sincronizar status %s (sanitizado)", order_id)
        flash("Erro interno ao sincronizar status.", "danger")

    return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))
