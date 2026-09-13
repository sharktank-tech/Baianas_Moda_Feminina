"""FASE 8.2.0-F — fato observável de recebimento líquido do pagamento.

Extrai exclusivamente do Payment persistido a relação entre valor cobrado,
valor líquido recebido e retenção total do processador. Não consulta provider,
não muta modelos e não tenta decompor a retenção entre tarifa e parcelamento.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


class PaymentNetReceiptError(ValueError):
    """Payload persistido presente, porém monetariamente incoerente."""


@dataclass(frozen=True)
class PaymentNetReceipt:
    charged_amount: Decimal
    net_received_amount: Decimal
    processor_retained_amount: Decimal
    currency: str


def _money(value, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentNetReceiptError(f"{field}_invalid") from None
    if amount < 0:
        raise PaymentNetReceiptError(f"{field}_negative")
    return amount


def resolve_payment_net_receipt(payment) -> Optional[PaymentNetReceipt]:
    """Resolve o recebimento líquido de um Payment Mercado Pago aprovado.

    Ausência do fato retorna None (unknown). Se os campos existem, divergências
    de valor/moeda e retenção negativa são erros de integridade.
    """
    if str(getattr(payment, "provider", "") or "").strip() != "mercado_pago":
        return None
    if str(getattr(payment, "status", "") or "").strip().lower() != "approved":
        return None

    payload = getattr(payment, "raw_payload", None)
    if not isinstance(payload, dict):
        return None
    details = payload.get("transaction_details")
    if not isinstance(details, dict):
        return None

    raw_payload_amount = payload.get("transaction_amount")
    raw_net = details.get("net_received_amount")
    raw_payload_currency = payload.get("currency_id")
    if (
        raw_payload_amount is None
        or raw_net is None
        or not raw_payload_currency
        or getattr(payment, "amount", None) is None
        or not getattr(payment, "currency", None)
    ):
        return None

    persisted_amount = _money(payment.amount, "payment_amount")
    payload_amount = _money(raw_payload_amount, "payload_transaction_amount")
    net_received = _money(raw_net, "payload_net_received_amount")
    if persisted_amount != payload_amount:
        raise PaymentNetReceiptError("payment_amount_mismatch")

    persisted_currency = str(payment.currency).strip().upper()
    payload_currency = str(raw_payload_currency).strip().upper()
    if (
        len(persisted_currency) != 3
        or len(payload_currency) != 3
        or persisted_currency != payload_currency
    ):
        raise PaymentNetReceiptError("payment_currency_mismatch")
    if net_received > persisted_amount:
        raise PaymentNetReceiptError("payment_net_received_exceeds_charged")

    return PaymentNetReceipt(
        charged_amount=persisted_amount,
        net_received_amount=net_received,
        processor_retained_amount=(
            persisted_amount - net_received
        ).quantize(Decimal("0.01")),
        currency=persisted_currency,
    )


__all__ = [
    "PaymentNetReceipt",
    "PaymentNetReceiptError",
    "resolve_payment_net_receipt",
]
