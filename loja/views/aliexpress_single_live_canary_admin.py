"""Admin-only live-canary surfaces for AliExpress placeorder.

FASE 8.0.11I keeps the FIRST live canary one-shot intact.
FASE 8.0.11K adds a SEPARATE second diagnostic canary for an explicitly
reconciled first attempt. Both GET endpoints are read-only; both POST
endpoints are protected by the application-wide CSRF guard and delegate only
to their respective fail-closed boundaries.

FASE 8.0.11P adds a THIRD-canary preflight surface. FASE 8.0.11S prepares its
POST surface behind an additional independent final execution hard-off gate.
The POST is admin-only, remains covered by the application-wide CSRF guard and
cannot reach the third-canary submit boundary while the S gate is OFF.

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
    AliExpressThirdDiagnosticCanaryConfigurationError,
    AliExpressThirdDiagnosticCanaryConsumed,
    AliExpressThirdDiagnosticCanaryPreflightError,
    get_third_diagnostic_canary_preflight,
)
from loja.integrations.aliexpress.third_diagnostic_execution import (
    AliExpressThirdDiagnosticExecutionHardOff,
    execute_third_diagnostic_canary_order,
    get_third_diagnostic_execution_status,
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
    """Read-only operational preflight plus S execution-layer status.

    Evaluating the O-boundary can touch local credential state through
    validation; rollback is guaranteed even if evaluation raises. The S status
    is environment-only and cannot arm itself.
    """

    ali_order = _load_order(order_id)
    try:
        status = get_third_diagnostic_canary_preflight(
            ali_order,
            session=db.session,
        )
    finally:
        db.session.rollback()

    execution_status = get_third_diagnostic_execution_status()
    ali_order = _load_order(order_id)
    return render_template(
        "admin/aliexpress/third_diagnostic_canary_preflight.html",
        ali_order=ali_order,
        canary_preflight=status,
        execution_status=execution_status,
    )


@single_live_canary_admin_blueprint.route(
    "/<uuid:order_id>/third-diagnostic-canary/submit", methods=["POST"]
)
@admin_required
def third_diagnostic_submit(order_id):
    """Prepared FASE 8.0.11S POST, hard-off by an independent final gate.

    This route never arms any gate. While the S execution authorization is OFF,
    it fails before delegating to the third-canary submit boundary, so no real
    provider resolution or HTTP can occur through this surface.
    """

    ali_order = _load_order(order_id)
    try:
        result = execute_third_diagnostic_canary_order(
            ali_order,
            session=db.session,
        )
        state = getattr(result, "state", None) or "unknown"
        if state == "submitted":
            flash(
                "Terceiro canário confirmado localmente. Desarme TODOS os gates imediatamente.",
                "success",
            )
        elif state == "pending_reconciliation":
            flash(
                "Terceiro canário ficou ambíguo. Desarme TODOS os gates e NÃO reenvie; reconciliação conclusiva é obrigatória.",
                "danger",
            )
        else:
            flash(
                "Terceiro canário encerrou sem confirmação de submissão. Desarme TODOS os gates e revise o estado durável.",
                "warning",
            )
    except AliExpressThirdDiagnosticExecutionHardOff:
        db.session.rollback()
        flash(
            "FASE 8.0.11S: execução final permanece HARD-OFF; nenhuma chamada ao AliExpress foi autorizada.",
            "danger",
        )
    except AliExpressThirdDiagnosticCanaryConsumed:
        db.session.rollback()
        flash(
            "Terceiro canário diagnóstico já consumido; qualquer nova submissão está bloqueada.",
            "danger",
        )
    except (
        AliExpressThirdDiagnosticCanaryConfigurationError,
        AliExpressThirdDiagnosticCanaryPreflightError,
        AliExpressCanaryConfigurationError,
        AliExpressCanaryCircuitOpen,
        SubmissionHardOffError,
    ):
        db.session.rollback()
        flash(
            "Terceiro canário bloqueado pelo preflight/gates de segurança.",
            "danger",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.error("third diagnostic canary: erro sanitizado")
        flash(
            "Terceiro canário interrompido por erro interno sanitizado. Não reenvie sem revisar o estado durável.",
            "danger",
        )

    return redirect(
        url_for(
            "aliexpress_single_live_canary_admin.third_diagnostic_preflight",
            order_id=order_id,
        ),
        code=303,
    )


__all__ = ["single_live_canary_admin_blueprint"]
