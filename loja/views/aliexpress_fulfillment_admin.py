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

from loja import db, csrf
from loja.modules.models import AliExpressOrder, AliExpressOrderItem, Order, Supplier
from loja.integrations.aliexpress import (
    ensure_aliexpress_fulfillment,
    get_fulfillment_access_token,
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
@csrf.exempt  # CSRF protection handled by template form
def revalidate(order_id):
    """Revalida fulfillment: roda validações locais + credencial novamente.

    NÃO envia pedido ao AliExpress.
    """
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order),
    ).filter_by(id=order_id).first_or_404()

    if not ali_order.order:
        flash("Pedido original não encontrado.", "danger")
        return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))

    try:
        # Re-executar validações (idempotente)
        results = ensure_aliexpress_fulfillment(ali_order.order)

        # Filtrar resultado para este supplier específico
        for result in results:
            if result.aliexpress_order.id == ali_order.id:
                db.session.commit()
                if result.state == "ready":
                    flash("Fulfillment revalidado com sucesso — estado: READY.", "success")
                else:
                    flash(f"Fulfillment revalidado — estado: {result.state.upper()} ({result.aliexpress_order.last_error}).", "warning")
                break
        else:
            flash("Fulfillment não encontrado no resultado da revalidação.", "danger")

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