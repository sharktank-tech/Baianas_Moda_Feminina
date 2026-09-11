"""Admin-only live-canary surfaces for AliExpress placeorder.

FASE 8.0.11I keeps the FIRST live canary one-shot intact.
FASE 8.0.11K adds a SEPARATE second diagnostic canary for an explicitly
reconciled first attempt. Both GET endpoints are read-only; both POST
endpoints are protected by the application-wide CSRF guard and delegate only
to their respective fail-closed boundaries.

FASE 8.0.11P adds a THIRD-canary preflight surface that is GET-only. It has no
submit route and imports no third-canary submit function. The route explicitly
rolls back after evaluating the preflight so any incidental local credential
state flush performed by validation cannot survive the read-only request.

No route automatically selects an order or exposes provider secrets/payloads.
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
from loja.integrations.aliexpress.second_diagnostic_canary import (
    AliExpressSecondDiagnosticCanaryConfigurationError,
    AliExpressSecondDiagnosticCanaryConsumed,
    AliExpressSecondDiagnosticCanaryPreflightError,
    get_second_diagnostic_canary_preflight,
    submit_second_diagnostic_canary_order,
)
from loja.integrations.aliexpress.single_live_canary import (
    AliExpressSingleLiveCanaryConfigurationError,
    AliExpressSingleLiveCanaryConsumed,
    AliExpressSingleLiveCanaryPreflightError,
    get_single_live_canary_preflight,
    submit_single_live_canary_order,
)
from loja.integrations.aliexpress.third_diagnostic_canary import (
    get_third_diagnostic_canary_preflight,
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
    """Read-only preflight for the first explicitly selected local order."""

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
    """Execute at most the explicitly armed FIRST one-shot canary."""

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


@single_live_canary_admin_blueprint.route(
    "/<uuid:order_id>/second-diagnostic-canary", methods=["GET"]
)
@admin_required
def second_diagnostic_preflight(order_id):
    """Read-only preflight for FASE 8.0.11K.

    The endpoint never arms gates and never obtains the real provider.
    """

    ali_order = _load_order(order_id)
    status = get_second_diagnostic_canary_preflight(ali_order, session=db.session)
    return render_template(
        "admin/aliexpress/second_diagnostic_canary.html",
        ali_order=ali_order,
        canary_preflight=status,
    )


@single_live_canary_admin_blueprint.route(
    "/<uuid:order_id>/second-diagnostic-canary/submit", methods=["POST"]
)
@admin_required
def second_diagnostic_submit(order_id):
    """Execute at most the separately armed SECOND diagnostic attempt.

    The route never retries. Unknown remains pending_reconciliation and any
    second durable attempt permanently consumes the FASE 8.0.11K slot.
    """

    ali_order = _load_order(order_id)
    try:
        result = submit_second_diagnostic_canary_order(ali_order, session=db.session)
        state = getattr(result, "state", None) or "unknown"
        if state == "submitted":
            flash(
                "Segundo canário diagnóstico confirmado localmente. Desarme todos os gates agora.",
                "success",
            )
        elif state == "pending_reconciliation":
            flash(
                "Segundo canário ficou ambíguo. Desarme os gates e NÃO reenvie; nova reconciliação conclusiva é obrigatória.",
                "danger",
            )
        else:
            flash(
                "Segundo canário encerrou sem confirmação de submissão. Desarme os gates e revise o estado durável.",
                "warning",
            )
    except AliExpressSecondDiagnosticCanaryConsumed:
        db.session.rollback()
        flash(
            "Segundo canário diagnóstico já consumido; qualquer nova submissão está bloqueada.",
            "danger",
        )
    except (
        AliExpressSecondDiagnosticCanaryConfigurationError,
        AliExpressSecondDiagnosticCanaryPreflightError,
        AliExpressCanaryConfigurationError,
        AliExpressCanaryCircuitOpen,
        SubmissionHardOffError,
    ):
        db.session.rollback()
        flash(
            "Segundo canário bloqueado pelo preflight/gates de segurança.",
            "danger",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.error("second diagnostic canary: erro sanitizado")
        flash(
            "Segundo canário interrompido por erro interno sanitizado. Não reenvie sem revisar o estado durável.",
            "danger",
        )

    return redirect(
        url_for(
            "aliexpress_single_live_canary_admin.second_diagnostic_preflight",
            order_id=order_id,
        ),
        code=303,
    )


@single_live_canary_admin_blueprint.route(
    "/<uuid:order_id>/third-diagnostic-canary", methods=["GET"]
)
@admin_required
def third_diagnostic_preflight(order_id):
    """GET-only operational preflight for FASE 8.0.11P.

    There is intentionally NO POST/submit route for the third canary in this
    phase. Evaluating the existing O-boundary can touch local credential state
    through validation; a rollback immediately after evaluation guarantees the
    admin GET remains persistence read-only. The order is then reloaded for
    rendering.
    """

    ali_order = _load_order(order_id)
    status = get_third_diagnostic_canary_preflight(ali_order, session=db.session)

    # Fail-closed read-only guarantee for this GET surface: even if credential
    # validation performed an incidental flush, nothing from the preflight is
    # allowed to persist.
    db.session.rollback()
    ali_order = _load_order(order_id)

    return render_template(
        "admin/aliexpress/third_diagnostic_canary_preflight.html",
        ali_order=ali_order,
        canary_preflight=status,
    )


__all__ = ["single_live_canary_admin_blueprint"]
