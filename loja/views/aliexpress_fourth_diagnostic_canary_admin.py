"""Admin-only fourth diagnostic AliExpress canary — FASE 8.0.11V.

GET is read-only. POST is covered by the app-wide CSRF guard and delegates only
to the independent V execution boundary. No route arms environment gates or
automatically selects an order.
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
from loja.integrations.aliexpress.fourth_diagnostic_canary import (
    AliExpressFourthDiagnosticCanaryConfigurationError,
    AliExpressFourthDiagnosticCanaryConsumed,
    AliExpressFourthDiagnosticCanaryPreflightError,
    get_fourth_diagnostic_canary_preflight,
)
from loja.integrations.aliexpress.fourth_diagnostic_execution import (
    AliExpressFourthDiagnosticExecutionHardOff,
    execute_fourth_diagnostic_canary_order,
    get_fourth_diagnostic_execution_status,
)
from loja.integrations.aliexpress.submission import SubmissionHardOffError
from loja.utils.decorators import admin_required


fourth_diagnostic_canary_admin_blueprint = Blueprint(
    "aliexpress_fourth_diagnostic_canary_admin",
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


@fourth_diagnostic_canary_admin_blueprint.route(
    "/<uuid:order_id>/fourth-diagnostic-canary", methods=["GET"]
)
@admin_required
def preflight(order_id):
    ali_order = _load_order(order_id)
    try:
        status = get_fourth_diagnostic_canary_preflight(ali_order, session=db.session)
    finally:
        db.session.rollback()

    execution_status = get_fourth_diagnostic_execution_status()
    ali_order = _load_order(order_id)
    return render_template(
        "admin/aliexpress/fourth_diagnostic_canary_preflight.html",
        ali_order=ali_order,
        canary_preflight=status,
        execution_status=execution_status,
    )


@fourth_diagnostic_canary_admin_blueprint.route(
    "/<uuid:order_id>/fourth-diagnostic-canary/submit", methods=["POST"]
)
@admin_required
def submit(order_id):
    ali_order = _load_order(order_id)
    try:
        result = execute_fourth_diagnostic_canary_order(ali_order, session=db.session)
        state = getattr(result, "state", None) or "unknown"
        if state == "submitted":
            flash(
                "Quarto canário confirmado localmente. Desarme TODOS os gates imediatamente.",
                "success",
            )
        elif state == "pending_reconciliation":
            flash(
                "Quarto canário ficou ambíguo. Desarme TODOS os gates e NÃO reenvie; reconciliação conclusiva é obrigatória.",
                "danger",
            )
        else:
            flash(
                "Quarto canário encerrou sem confirmação de submissão. Desarme TODOS os gates e revise o estado durável.",
                "warning",
            )
    except AliExpressFourthDiagnosticExecutionHardOff:
        db.session.rollback()
        flash(
            "FASE 8.0.11V: execução final permanece HARD-OFF; nenhuma chamada ao AliExpress foi realizada.",
            "danger",
        )
    except AliExpressFourthDiagnosticCanaryConsumed:
        db.session.rollback()
        flash("Quarto canário já consumido; nova submissão bloqueada.", "danger")
    except (
        AliExpressFourthDiagnosticCanaryConfigurationError,
        AliExpressFourthDiagnosticCanaryPreflightError,
        AliExpressCanaryConfigurationError,
        AliExpressCanaryCircuitOpen,
        SubmissionHardOffError,
    ):
        db.session.rollback()
        flash("Quarto canário bloqueado pelo preflight/gates de segurança.", "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.error("fourth diagnostic canary: erro sanitizado")
        flash(
            "Quarto canário interrompido por erro interno sanitizado. Não reenvie sem revisar o estado durável.",
            "danger",
        )

    return redirect(
        url_for("aliexpress_fourth_diagnostic_canary_admin.preflight", order_id=order_id),
        code=303,
    )


__all__ = ["fourth_diagnostic_canary_admin_blueprint"]
