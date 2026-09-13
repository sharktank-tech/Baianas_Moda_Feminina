"""Ledger e reconciliação de refund pós-cancelamento (FASE 8.3.0-H).

Nenhuma função chama o gateway. O ledger registra intenção/desfecho do boundary,
mas somente o webhook Mercado Pago pode confirmar o estado financeiro refunded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loja import db
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.utils import utcnow


@dataclass(frozen=True)
class SupplierRefundLedgerDecision:
    state: Optional[str]
    changed: bool
    reason: Optional[str] = None


def _locked_order(order, session):
    locked = (
        session.query(Order)
        .filter_by(id=order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked is None:
        raise ValueError("order_not_found")
    return locked


def record_supplier_refund_request(order, *, session=None):
    """Registra preflight elegível antes do boundary financeiro."""
    session = session or db.session
    locked = _locked_order(order, session)
    if locked.status == "refunded":
        return reconcile_supplier_refund_from_webhook(locked, session=session)
    if locked.status != "paid":
        session.commit()
        return SupplierRefundLedgerDecision(
            locked.supplier_refund_state,
            False,
            "refund_order_state_not_eligible",
        )

    fulfillments = tuple(locked.aliexpress_orders or ())
    if not fulfillments or any(item.state != "cancelled" for item in fulfillments):
        session.commit()
        return SupplierRefundLedgerDecision(
            locked.supplier_refund_state,
            False,
            "refund_requires_all_fulfillments_cancelled",
        )

    if locked.supplier_refund_state in {"requested", "pending_webhook"}:
        session.commit()
        return SupplierRefundLedgerDecision(
            locked.supplier_refund_state, False
        )
    if locked.supplier_refund_state == "confirmed":
        session.commit()
        return SupplierRefundLedgerDecision("confirmed", False)

    now = utcnow()
    locked.supplier_refund_state = "requested"
    locked.supplier_refund_requested_at = (
        locked.supplier_refund_requested_at or now
    )
    locked.supplier_refund_last_error = None
    locked.updated_at = now
    session.commit()
    return SupplierRefundLedgerDecision("requested", True)


def record_supplier_refund_boundary_outcome(
    order,
    *,
    outcome: str,
    session=None,
):
    """Persiste outcome canônico sem alterar Order.status."""
    session = session or db.session
    locked = _locked_order(order, session)
    if locked.supplier_refund_state not in {"requested", "pending_webhook"}:
        session.commit()
        return SupplierRefundLedgerDecision(
            locked.supplier_refund_state,
            False,
            "refund_not_requested",
        )

    now = utcnow()
    reason = None
    if outcome == "confirmed":
        locked.supplier_refund_state = "pending_webhook"
    elif outcome == "rejected":
        locked.supplier_refund_state = "failed"
        reason = "refund_rejected"
    elif outcome == "unknown":
        locked.supplier_refund_state = "manual_review"
        reason = "refund_unknown"
    else:
        session.commit()
        return SupplierRefundLedgerDecision(
            locked.supplier_refund_state,
            False,
            "refund_outcome_invalid",
        )
    locked.supplier_refund_last_error = reason
    locked.updated_at = now
    session.commit()
    return SupplierRefundLedgerDecision(
        locked.supplier_refund_state, True, reason
    )


def reconcile_supplier_refund_from_webhook(order, *, session=None):
    """Confirma ledger somente quando Order.status já é refunded."""
    session = session or db.session
    locked = _locked_order(order, session)
    if locked.status != "refunded":
        session.commit()
        return SupplierRefundLedgerDecision(
            locked.supplier_refund_state,
            False,
            "refund_webhook_not_confirmed",
        )

    # Refunds alheios ao fluxo de cancelamento do fornecedor não criam ledger.
    if locked.supplier_refund_state is None:
        session.commit()
        return SupplierRefundLedgerDecision(
            None, False, "supplier_refund_not_applicable"
        )
    if locked.supplier_refund_state == "confirmed":
        session.commit()
        return SupplierRefundLedgerDecision("confirmed", False)

    now = utcnow()
    locked.supplier_refund_state = "confirmed"
    locked.supplier_refund_confirmed_at = (
        locked.supplier_refund_confirmed_at or now
    )
    locked.supplier_refund_last_error = None
    locked.updated_at = now
    session.commit()
    return SupplierRefundLedgerDecision("confirmed", True)
