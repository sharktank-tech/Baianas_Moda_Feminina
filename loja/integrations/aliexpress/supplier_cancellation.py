"""Máquina pós-venda de cancelamento/rejeição (FASE 8.3.0-F).

Somente transições internas e resultados semânticos sanitizados. Este módulo
não chama provider, não dispara refund e não aceita mensagens remotas livres.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loja import db
from loja.modules.models import AliExpressOrder
from loja.utils import utcnow


CANCEL_CONFIRMED = "cancelled"
CANCEL_REJECTED = "rejected"
CANCEL_UNKNOWN = "unknown"
_ALLOWED_OUTCOMES = frozenset(
    {CANCEL_CONFIRMED, CANCEL_REJECTED, CANCEL_UNKNOWN}
)
_REQUESTABLE_STATES = frozenset({"submitted", "confirmed"})


@dataclass(frozen=True)
class SupplierCancellationDecision:
    state: str
    changed: bool
    reason: Optional[str] = None


def _locked_order(ali_order, session):
    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if order is None:
        raise ValueError("aliexpress_order_not_found")
    return order


def request_supplier_cancellation(
    ali_order: AliExpressOrder,
    *,
    session=None,
) -> SupplierCancellationDecision:
    """Registra intenção local; nenhuma chamada remota ocorre."""
    session = session or db.session
    order = _locked_order(ali_order, session)

    if order.state == "cancelled":
        session.commit()
        return SupplierCancellationDecision("cancelled", False)
    if order.state == "cancel_requested":
        session.commit()
        return SupplierCancellationDecision("cancel_requested", False)
    if order.state not in _REQUESTABLE_STATES:
        session.commit()
        return SupplierCancellationDecision(
            order.state,
            False,
            "supplier_cancellation_state_not_eligible",
        )

    order.state = "cancel_requested"
    order.last_error = None
    order.updated_at = utcnow()
    session.commit()
    return SupplierCancellationDecision("cancel_requested", True)


def apply_supplier_cancellation_result(
    ali_order: AliExpressOrder,
    *,
    outcome: str,
    session=None,
) -> SupplierCancellationDecision:
    """Aplica desfecho canônico; nunca recebe mensagem bruta do provider."""
    session = session or db.session
    order = _locked_order(ali_order, session)

    if outcome not in _ALLOWED_OUTCOMES:
        session.commit()
        return SupplierCancellationDecision(
            order.state, False, "supplier_cancellation_outcome_invalid"
        )
    if order.state == "cancelled":
        session.commit()
        return SupplierCancellationDecision("cancelled", False)
    if order.state != "cancel_requested":
        session.commit()
        return SupplierCancellationDecision(
            order.state, False, "supplier_cancellation_not_requested"
        )

    now = utcnow()
    if outcome == CANCEL_CONFIRMED:
        order.state = "cancelled"
        order.cancelled_at = order.cancelled_at or now
        order.last_error = None
        reason = None
    elif outcome == CANCEL_REJECTED:
        order.state = "manual_review"
        order.last_error = "supplier_cancellation_rejected"
        reason = "supplier_cancellation_rejected"
    else:
        # Ambiguidade não é tratada como sucesso ou retry seguro.
        order.last_error = "supplier_cancellation_unknown"
        reason = "supplier_cancellation_unknown"

    order.updated_at = now
    session.commit()
    return SupplierCancellationDecision(order.state, True, reason)


def record_supplier_rejection(
    ali_order: AliExpressOrder,
    *,
    session=None,
) -> SupplierCancellationDecision:
    """Registra rejeição conclusiva do pedido sem propagar motivo remoto."""
    session = session or db.session
    order = _locked_order(ali_order, session)

    if order.state in {"cancelled", "delivered"}:
        session.commit()
        return SupplierCancellationDecision(
            order.state, False, "supplier_rejection_terminal_state"
        )
    if order.state == "manual_review" and order.last_error == "supplier_rejected":
        session.commit()
        return SupplierCancellationDecision("manual_review", False)

    order.state = "manual_review"
    order.last_error = "supplier_rejected"
    order.updated_at = utcnow()
    session.commit()
    return SupplierCancellationDecision(
        "manual_review", True, "supplier_rejected"
    )
