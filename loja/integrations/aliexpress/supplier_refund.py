"""Boundary fail-closed de reembolso pós-cancelamento (FASE 8.3.0-G).

O estado financeiro refunded continua sendo autoridade do webhook Mercado Pago.
Este módulo apenas valida elegibilidade e oferece um boundary explicitamente
injetável. O default é HARD-OFF e não realiza operação financeira.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


REFUND_ENABLE_ENV = "SUPPLIER_CANCELLATION_REFUND_ENABLED"
REFUND_CONFIRMED = "confirmed"
REFUND_REJECTED = "rejected"
REFUND_UNKNOWN = "unknown"
_ALLOWED_OUTCOMES = frozenset(
    {REFUND_CONFIRMED, REFUND_REJECTED, REFUND_UNKNOWN}
)


class SupplierCancellationRefundUnavailable(Exception):
    """Boundary financeiro indisponível; mensagem fixa e sanitizada."""


@dataclass(frozen=True)
class SupplierCancellationRefundRequest:
    order_id: object
    provider_payment_id: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class SupplierCancellationRefundResult:
    outcome: str


@dataclass(frozen=True)
class SupplierCancellationRefundDecision:
    state: str
    reason: Optional[str] = None


class SupplierCancellationRefundProvider(ABC):
    @abstractmethod
    def refund(
        self, request: SupplierCancellationRefundRequest
    ) -> SupplierCancellationRefundResult:
        """Solicita refund; implementação real fica fora deste checkpoint."""


class HardOffSupplierCancellationRefundProvider(
    SupplierCancellationRefundProvider
):
    def refund(self, request):
        raise SupplierCancellationRefundUnavailable(
            "supplier_cancellation_refund_hard_off"
        )


def get_default_refund_provider():
    return HardOffSupplierCancellationRefundProvider()


def is_supplier_cancellation_refund_enabled() -> bool:
    return os.environ.get(REFUND_ENABLE_ENV) == "1"


def _eligible_request(order) -> tuple[Optional[SupplierCancellationRefundRequest], str]:
    fulfillments = tuple(getattr(order, "aliexpress_orders", None) or ())
    if not fulfillments:
        return None, "refund_fulfillment_missing"
    if any(getattr(item, "state", None) != "cancelled" for item in fulfillments):
        return None, "refund_requires_all_fulfillments_cancelled"

    status = str(getattr(order, "status", "") or "")
    if status == "refunded":
        return None, "refund_already_confirmed"
    if status != "paid":
        return None, "refund_order_state_not_eligible"

    payments = [
        payment
        for payment in tuple(getattr(order, "payments", None) or ())
        if getattr(payment, "status", None) == "approved"
    ]
    if len(payments) != 1:
        return None, "refund_payment_cardinality_invalid"

    payment = payments[0]
    payment_id = str(getattr(payment, "provider_payment_id", "") or "").strip()
    currency = str(getattr(payment, "currency", "") or "").strip().upper()
    try:
        amount = Decimal(str(getattr(payment, "amount", None)))
        order_total = Decimal(str(getattr(order, "total", None)))
    except Exception:
        return None, "refund_amount_invalid"

    if (
        not payment_id
        or currency != "BRL"
        or not amount.is_finite()
        or amount <= 0
        or amount != order_total
    ):
        return None, "refund_payment_mismatch"

    return (
        SupplierCancellationRefundRequest(
            order_id=order.id,
            provider_payment_id=payment_id,
            amount=amount,
            currency=currency,
        ),
        "",
    )


def evaluate_supplier_cancellation_refund(order):
    """Preflight puro; não chama provider e não altera estado."""
    request, reason = _eligible_request(order)
    if request is None:
        state = "completed" if reason == "refund_already_confirmed" else "blocked"
        return SupplierCancellationRefundDecision(state, reason), None
    return SupplierCancellationRefundDecision("eligible"), request


def request_supplier_cancellation_refund(
    order,
    *,
    provider: Optional[SupplierCancellationRefundProvider] = None,
):
    """Executa boundary somente sob gate; não persiste refunded por antecipação."""
    decision, request = evaluate_supplier_cancellation_refund(order)
    if request is None:
        return decision
    if not is_supplier_cancellation_refund_enabled():
        return SupplierCancellationRefundDecision(
            "blocked", "supplier_cancellation_refund_hard_off"
        )
    if provider is None:
        return SupplierCancellationRefundDecision(
            "blocked", "refund_provider_required"
        )

    try:
        result = provider.refund(request)
    except SupplierCancellationRefundUnavailable:
        return SupplierCancellationRefundDecision(
            "failed", "refund_provider_unavailable"
        )
    except Exception:
        return SupplierCancellationRefundDecision(
            "failed", "refund_provider_error"
        )

    outcome = getattr(result, "outcome", None)
    if outcome not in _ALLOWED_OUTCOMES:
        return SupplierCancellationRefundDecision(
            "failed", "refund_outcome_invalid"
        )
    if outcome == REFUND_CONFIRMED:
        # Confirmação financeira definitiva continua sendo reconciliada pelo
        # webhook existente; este retorno não falsifica Order.status.
        return SupplierCancellationRefundDecision(
            "pending_webhook", "refund_waiting_webhook"
        )
    if outcome == REFUND_REJECTED:
        return SupplierCancellationRefundDecision("failed", "refund_rejected")
    return SupplierCancellationRefundDecision("pending", "refund_unknown")
