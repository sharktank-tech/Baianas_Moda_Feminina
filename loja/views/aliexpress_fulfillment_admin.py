"""Fila administrativa de fulfillment AliExpress (Fase 8.0).

Segurança:
  * Todas as rotas exigem @admin_required + CSRF
  * Nunca expõe tokens, ciphertext, supplier_sku_id, product_id externo ou payload bruto
  * Enriquecimento SKU operacional é Preview-only e mostra apenas contagens
"""

from __future__ import annotations

import os

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
from sqlalchemy import desc
from sqlalchemy.orm import selectinload

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressOrderItem, Order, Supplier
from loja.integrations.aliexpress import (
    ensure_aliexpress_fulfillment,
    sync_aliexpress_order_status,
    CredentialNotConfigured,
    CredentialNotFound,
    CredentialExpired,
    CredentialInvalid,
)
from loja.integrations.aliexpress.sku_identity_enrichment import (
    SkuIdentityEnrichmentError,
    enrich_sku_identity_batch,
    get_sku_identity_coverage,
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
        "pending_reconciliation", "confirmed", "shipped", "delivered",
        "cancel_requested", "cancelled", "failed", "manual_review", "pending",
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
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.product),
        selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.variant),
        selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.order_item),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order).selectinload(Order.user),
    ).filter_by(id=order_id).first_or_404()

    return render_template("admin/aliexpress/fulfillment_detail.html", ali_order=ali_order)


@fulfillment_admin_blueprint.route("/<uuid:order_id>/revalidar", methods=["POST"])
@admin_required
def revalidate(order_id):
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
        flash("Access token expirado.", "danger")
    except CredentialInvalid:
        db.session.rollback()
        flash("Credencial inválida.", "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.error("Erro ao revalidar fulfillment (sanitizado)")
        flash("Erro interno ao revalidar fulfillment.", "danger")

    return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))


@fulfillment_admin_blueprint.route("/<uuid:order_id>/sincronizar-status", methods=["POST"])
@admin_required
def sync_status(order_id):
    ali_order = AliExpressOrder.query.options(
        selectinload(AliExpressOrder.items),
        selectinload(AliExpressOrder.supplier),
        selectinload(AliExpressOrder.order),
    ).filter_by(id=order_id).first_or_404()

    eligible = (
        ali_order.ali_order_id
        and ali_order.state in ("submitted", "confirmed", "shipped", "delivered")
    )
    if not eligible:
        flash(
            f"Fulfillment em estado {ali_order.state} não pode ter status sincronizado.",
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
        current_app.logger.error("Erro ao sincronizar status (sanitizado)")
        flash("Erro interno ao sincronizar status.", "danger")

    return redirect(url_for("aliexpress_fulfillment_admin.detail", order_id=order_id))


# --------------------------------------------------------------------------
# Fase 8.0.10 — enriquecimento operacional SKU, Preview-only
# --------------------------------------------------------------------------
_SKU_ENRICH_TEMPLATE = """
<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>AliExpress SKU identity enrichment</title></head>
<body style="font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px">
<h1>SKU identity enrichment</h1>
<p>Preview only · read-only na AliExpress · escrita local apenas em ProductVariant.attributes.</p>
<p><strong>Total:</strong> {{ coverage.total }}</p>
<p><strong>Completas:</strong> {{ coverage.complete }}</p>
<p><strong>Pendentes:</strong> {{ coverage.incomplete }}</p>
{% if result %}
<hr>
<p><strong>Produtos processados:</strong> {{ result.processed_products }}</p>
<p><strong>Variantes atualizadas:</strong> {{ result.updated_variants }}</p>
<p><strong>Variantes não resolvidas:</strong> {{ result.unresolved_variants }}</p>
<p><strong>Produtos com falha:</strong> {{ result.failed_products }}</p>
{% endif %}
{% if error %}<p><strong>Erro:</strong> {{ error }}</p>{% endif %}
{% if coverage.incomplete > 0 %}
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button type="submit">Processar próximo produto</button>
</form>
<p><small>Uma única chamada product.get por clique; timeout HTTP operacional de 5 s.</small></p>
{% else %}<p><strong>Enriquecimento concluído.</strong></p>{% endif %}
</body></html>
"""


def _assert_sku_enrichment_preview():
    if os.environ.get("VERCEL_ENV") != "preview":
        abort(404)
    if os.environ.get("VERCEL_GIT_COMMIT_REF") != "feature/aliexpress-fulfillment-core":
        abort(404)


@fulfillment_admin_blueprint.route(
    "/sku-identity-enrichment", methods=["GET", "POST"], strict_slashes=False
)
@admin_required
def sku_identity_enrichment():
    _assert_sku_enrichment_preview()
    result = None
    error = None

    if request.method == "POST":
        try:
            result = enrich_sku_identity_batch(batch_size=1)
        except SkuIdentityEnrichmentError as exc:
            db.session.rollback()
            error = str(exc)
        except Exception:
            db.session.rollback()
            error = "internal_error"

    coverage = get_sku_identity_coverage()
    return render_template_string(
        _SKU_ENRICH_TEMPLATE,
        coverage=coverage,
        result=result,
        error=error,
    )
