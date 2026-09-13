"""Fila administrativa de fulfillment AliExpress (Fase 8.0/8.1.0).

Segurança:
  * Todas as rotas exigem @admin_required + CSRF quando mutáveis
  * Nunca expõe tokens, ciphertext, supplier_sku_id, product_id externo ou payload bruto
  * Enriquecimento SKU operacional é Preview-only e mostra apenas contagens
  * Fase 8.0.11H expõe somente estado sanitizado do circuit breaker; attempt_key
    e remote_order_id nunca aparecem na fila de segurança
  * Fase 8.0.11N expõe no detalhe apenas um ledger sanitizado das tentativas:
    estado, timestamps e diagnóstico durável que ainda passe pelo parser
    fail-closed. IDs de tentativa/attempt_key nunca são enviados ao template.
  * Fase 8.1.0 expõe somente booleanos dos gates, estado/código sanitizado do
    breaker de produção e contagens agregadas. Valores crus de env não chegam ao HTML.
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
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    AliExpressSubmissionAttempt,
    Order,
    Supplier,
)
from loja.integrations.aliexpress import (
    ensure_aliexpress_fulfillment,
    sync_aliexpress_order_status,
    CredentialNotConfigured,
    CredentialNotFound,
    CredentialExpired,
    CredentialInvalid,
)
from loja.integrations.aliexpress.controlled_canary import get_canary_circuit_status
from loja.integrations.aliexpress.placeorder_diagnostics import validate_durable_unknown_error
from loja.integrations.aliexpress.production_observability import (
    get_production_observability_snapshot,
)
from loja.services.post_sale_exceptions import (
    build_post_sale_exception_snapshot,
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


def _sanitized_attempt_ledger(order_id):
    """Retorna somente campos operacionais seguros de tentativas duráveis."""
    attempts = (
        AliExpressSubmissionAttempt.query.filter_by(aliexpress_order_id=order_id)
        .order_by(AliExpressSubmissionAttempt.started_at.asc())
        .all()
    )
    return [
        {
            "state": attempt.state,
            "started_at": attempt.started_at,
            "completed_at": attempt.completed_at,
            "diagnostic": validate_durable_unknown_error(attempt.last_error),
        }
        for attempt in attempts
    ]


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

    canary_status = get_canary_circuit_status(session=db.session)
    production_status = get_production_observability_snapshot(session=db.session)

    return render_template(
        "admin/aliexpress/fulfillment_list.html",
        pagination=pagination,
        suppliers=suppliers,
        states=states,
        current_state=state_filter,
        current_supplier=supplier_filter,
        canary_status=canary_status,
        production_status=production_status,
        pending_reconciliation_count=production_status.pending_reconciliation_count,
    )


@fulfillment_admin_blueprint.route("/exceptions", methods=["GET"])
@admin_required
def post_sale_exceptions():
    """Fila read-only de exceções e alertas de estagnação."""
    fulfillments = (
        AliExpressOrder.query.options(
            selectinload(AliExpressOrder.supplier),
            selectinload(AliExpressOrder.order),
            selectinload(AliExpressOrder.remote_orders),
        )
        .order_by(AliExpressOrder.updated_at.asc())
        .all()
    )
    try:
        snapshot = build_post_sale_exception_snapshot(fulfillments)
        config_error = None
    except ValueError:
        snapshot = None
        config_error = "fulfillment_stale_after_invalid"

    return render_template(
        "admin/aliexpress/post_sale_exceptions.html",
        snapshot=snapshot,
        config_error=config_error,
    )


@fulfillment_admin_blueprint.route("/canary-safety", methods=["GET"])
@admin_required
def canary_safety():
    status = get_canary_circuit_status(session=db.session)
    blocking_rows = (
        db.session.query(AliExpressOrder, AliExpressSubmissionAttempt)
        .join(
            AliExpressSubmissionAttempt,
            AliExpressSubmissionAttempt.aliexpress_order_id == AliExpressOrder.id,
        )
        .filter(AliExpressSubmissionAttempt.state.in_(["in_flight", "unknown"]))
        .order_by(desc(AliExpressSubmissionAttempt.started_at))
        .limit(100)
        .all()
    )
    pending_reconciliation_count = AliExpressOrder.query.filter_by(
        state="pending_reconciliation"
    ).count()

    return render_template(
        "admin/aliexpress/canary_safety.html",
        canary_status=status,
        blocking_rows=blocking_rows,
        pending_reconciliation_count=pending_reconciliation_count,
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

    return render_template(
        "admin/aliexpress/fulfillment_detail.html",
        ali_order=ali_order,
        attempt_ledger=_sanitized_attempt_ledger(order_id),
    )


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


_SKU_ENRICH_TEMPLATE = """
<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>AliExpress SKU identity enrichment</title></head>
<body style="font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px">
<h1>SKU identity enrichment</h1>
<p>Preview only · read-only na AliExpress · escrita local apenas em ProductVariant.attributes.</p>
<p><strong>Total:</strong> {{ coverage.total }}</p>
<p><strong>Completas:</strong> {{ coverage.complete }}</p>
<p><strong>Pendentes:</strong> {{ coverage.incomplete }}</p>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for category, message in messages %}
    <p><strong>{{ message }}</strong></p>
  {% endfor %}
{% endwith %}
{% if coverage.incomplete > 0 %}
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button type="submit">Processar próximo produto</button>
</form>
<p><small>POST: uma chamada product.get + commit. Cobertura é recalculada somente no GET após redirect.</small></p>
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

    if request.method == "POST":
        try:
            result = enrich_sku_identity_batch(batch_size=1, include_coverage=False)
            if result.failed_products:
                flash("Produto não processado; falha sanitizada no provider.", "danger")
            elif result.unresolved_variants:
                flash(
                    f"Produto processado, mas {result.unresolved_variants} variante(s) "
                    "não foram resolvidas.",
                    "warning",
                )
            elif result.processed_products:
                flash(
                    f"Produto processado; {result.updated_variants} variante(s) atualizadas.",
                    "success",
                )
            else:
                flash("Nenhum produto pendente encontrado.", "info")
        except SkuIdentityEnrichmentError:
            db.session.rollback()
            flash("Enriquecimento indisponível por configuração/credencial.", "danger")
        except Exception:
            db.session.rollback()
            flash("Erro interno sanitizado no enriquecimento.", "danger")

        return redirect(
            url_for("aliexpress_fulfillment_admin.sku_identity_enrichment"),
            code=303,
        )

    coverage = get_sku_identity_coverage()
    return render_template_string(_SKU_ENRICH_TEMPLATE, coverage=coverage)
