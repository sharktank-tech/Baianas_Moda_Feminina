"""Admin-only OFFLINE CPF correction for AliExpress submission snapshots.

FASE 8.0.12B exposes the audited FASE 8.0.12A correction through the app so
sensitive CPF data does not need to be injected through database tooling.

Safety contract:
- admin-only + app-wide CSRF protection;
- GET is read-only and never returns the CPF value;
- POST accepts exactly one explicit CPF input and delegates to
  ``revise_submission_snapshot_cpf``;
- no provider/HTTP call, no SubmissionAttempt, no attempt_count/state change;
- errors and flash messages are sanitized and never echo the CPF.
"""

from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressOrderItem, Order
from loja.integrations.aliexpress.submission_snapshot import SubmissionSnapshotError
from loja.integrations.aliexpress.submission_snapshot_tax_id import revise_submission_snapshot_cpf
from loja.utils.decorators import admin_required


cpf_snapshot_admin_blueprint = Blueprint(
    "aliexpress_cpf_snapshot_admin",
    __name__,
    url_prefix="/admin/integrations/aliexpress/pedidos",
)


def _load_order(order_id):
    return (
        AliExpressOrder.query.options(
            selectinload(AliExpressOrder.items).selectinload(AliExpressOrderItem.product),
            selectinload(AliExpressOrder.supplier),
            selectinload(AliExpressOrder.order).selectinload(Order.user),
            selectinload(AliExpressOrder.submission_snapshot),
        )
        .filter_by(id=order_id)
        .first_or_404()
    )


def _sanitized_status(ali_order):
    snapshot = ali_order.submission_snapshot
    shipping = dict(snapshot.shipping_address or {}) if snapshot is not None else {}
    revision_count = 0
    if snapshot is not None:
        from loja.integrations.aliexpress.submission_snapshot import AliExpressSubmissionSnapshotRevision
        revision_count = AliExpressSubmissionSnapshotRevision.query.filter_by(
            aliexpress_order_id=ali_order.id
        ).count()
    return {
        "state": ali_order.state,
        "attempt_count": int(ali_order.attempt_count or 0),
        "has_cpf": bool(str(shipping.get("cpf") or "").strip()),
        "revision_count": revision_count,
        "has_remote_identity": bool(ali_order.ali_order_id),
    }


@cpf_snapshot_admin_blueprint.route(
    "/<uuid:order_id>/cpf-snapshot", methods=["GET"]
)
@admin_required
def cpf_snapshot(order_id):
    ali_order = _load_order(order_id)
    status = _sanitized_status(ali_order)
    return render_template(
        "admin/aliexpress/cpf_snapshot.html",
        ali_order=ali_order,
        cpf_status=status,
    )


@cpf_snapshot_admin_blueprint.route(
    "/<uuid:order_id>/cpf-snapshot", methods=["POST"]
)
@admin_required
def cpf_snapshot_submit(order_id):
    ali_order = _load_order(order_id)
    cpf = request.form.get("cpf", "")

    try:
        revise_submission_snapshot_cpf(
            ali_order,
            cpf=cpf,
            session=db.session,
        )
        db.session.commit()
        flash(
            "CPF aplicado ao snapshot AliExpress com revisão auditável. Nenhuma submissão foi executada.",
            "success",
        )
    except SubmissionSnapshotError as exc:
        db.session.rollback()
        code = exc.args[0] if exc.args else "snapshot_revision_blocked"
        allowed = {
            "shipping_cpf_invalid",
            "snapshot_revision_no_changes",
            "snapshot_revision_state_ineligible",
            "snapshot_revision_remote_identity_present",
            "snapshot_revision_attempt_unresolved",
            "snapshot_revision_attempt_ledger_mismatch",
            "snapshot_revision_requires_address_rejection",
            "snapshot_revision_snapshot_missing",
            "aliexpress_order_missing",
        }
        safe_code = code if code in allowed else "snapshot_revision_blocked"
        flash(f"Correção de CPF bloqueada: {safe_code}.", "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.error("cpf snapshot correction: erro sanitizado")
        flash("Correção de CPF interrompida por erro interno sanitizado.", "danger")

    return redirect(
        url_for("aliexpress_cpf_snapshot_admin.cpf_snapshot", order_id=order_id),
        code=303,
    )


__all__ = ["cpf_snapshot_admin_blueprint"]
