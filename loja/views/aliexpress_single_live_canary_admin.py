"""Admin-only boundary for the FIRST real AliExpress placeorder canary.

The GET is read-only and requires an explicit AliExpressOrder UUID.  The POST
is CSRF-protected by the application-wide Flask-WTF protection and delegates
ONLY to ``submit_single_live_canary_order``.  No alternate real-provider path
is exposed here.
"""

from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, url_for
from sqlalchemy.orm import selectinload

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressOrderItem, Order
from loja.integrations.aliexpress.controlled_canary import (
    AliExpressCanaryCircuitOpen,
    AliExpressCanaryConfigurationError,
)
from loja.integrations.aliexpress.single_live_canary import (
    AliExpressSingleLiveCanaryConfigurationError,
    AliExpressSingleLiveCanaryConsumed,
    AliExpressSingleLiveCanaryPreflightError,
    get_single_live_canary_preflight,
    submit_single_live_canary_order,
)
from loja.integrations.aliexpress.submission import SubmissionHardOffError
from loja.utils.decorators import admin_required


single_live_canary_admin_blueprint = Blueprint(
    "aliexpress_single_live_canary_admin",
    __name__,
    url_prefix="/admin/integrations/aliexpress/pedidos",
)


def _load_order(order_id):
    return (
        AliExpressOrder.query.options(
            selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.product),
            selectinload(AliExpressOrder.supplier),
            selectinload(AliExpressOrder.order).selectinload(Order.user),
        )
        .filter_by(id=order_id)
        .first_or_404()
    )


@single_live_canary_admin_blueprint.route(
    "/<uuid:order_id>/single-live-canary", methods=["GET"]
)
@admin_required
def preflight(order_id):
    """Read-only preflight for one explicitly selected local order."""

    ali_order = _load_order(order_id)
    status = get_single_live_canary_preflight(ali_order, session=db.session)
    return render_template(
        "admin/aliexpress/single_live_canary.html",
        ali_order=ali_order,
        canary_preflight=status,
    )


@single_live_canary_admin_blueprint.route(
    "/<uuid:order_id>/single-live-canary/submit", methods=["POST"]
)
@admin_required
def submit(order_id):
    """Execute at most the explicitly armed one-shot canary.

    CSRF is enforced globally.  All failure messages are fixed/sanitized; the
    exception text is never logged because provider/DB exceptions may carry
    sensitive material in unforeseen cases.
    """

    ali_order = _load_order(order_id)
    try:
        result = submit_single_live_canary_order(ali_order, session=db.session)
        state = getattr(result, "state", None) or "unknown"
        if state == "submitted":
            flash(
                "Canário AliExpress concluído com confirmação local. Desarme os gates agora.",
                "success",
            )
        elif state == "pending_reconciliation":
            flash(
                "Canário com resultado ambíguo. Desarme os gates e NÃO reenvie; reconciliação conclusiva é obrigatória.",
                "danger",
            )
        else:
            flash(
                "Canário encerrado sem confirmação de submissão. Desarme os gates e revise o estado antes de qualquer nova ação.",
                "warning",
            )
    except AliExpressSingleLiveCanaryConsumed:
        db.session.rollback()
        flash("Canário one-shot já consumido; nova submissão bloqueada.", "danger")
    except (
        AliExpressSingleLiveCanaryConfigurationError,
        AliExpressSingleLiveCanaryPreflightError,
        AliExpressCanaryConfigurationError,
        AliExpressCanaryCircuitOpen,
        SubmissionHardOffError,
    ):
        db.session.rollback()
        flash("Canário bloqueado pelo preflight/gates de segurança.", "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.error("single live canary: erro sanitizado")
        flash(
            "Canário interrompido por erro interno sanitizado. Não reenvie sem revisar o estado durável.",
            "danger",
        )

    return redirect(
        url_for("aliexpress_single_live_canary_admin.preflight", order_id=order_id),
        code=303,
    )


__all__ = ["single_live_canary_admin_blueprint"]
