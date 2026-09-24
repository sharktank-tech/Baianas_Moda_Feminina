"""Resolvedor read-only de modalidade de pagamento a partir do Payment persistido.

Extrai exclusivamente do payload Mercado Pago persistido a contagem de
parcelas, quando presente e coerente, e devolve o cenário de unit economics
correspondente ("cash" ou "installment"). Não consulta provider, não muta
modelos e nunca infere modalidade sem evidência: ausência ou valor inválido
retornam None (desconhecido).

Evidência única aceita: ``raw_payload["installments"]`` como inteiro >= 1.
1 = à vista; >= 2 = parcelada. ``payment_type_id`` não é usado porque não
distingue crédito de 1x de débito nem comprova parcelamento sem a contagem.

Também expõe ``select_order_payment``, a regra de seleção do pagamento que
representa a cobrança de um pedido (aprovado, valor igual ao total, mais
recente). Falha fechada: sem candidato elegível retorna None.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from .unit_economics_policy import (
    PAYMENT_SCENARIO_CASH,
    PAYMENT_SCENARIO_INSTALLMENT,
)

MERCADO_PAGO_PROVIDER = "mercado_pago"


def resolve_payment_scenario(payment) -> Optional[str]:
    """Resolve a modalidade de um Payment Mercado Pago a partir do payload.

    Retorna ``PAYMENT_SCENARIO_CASH``, ``PAYMENT_SCENARIO_INSTALLMENT`` ou
    ``None`` quando não há evidência utilizável. O status do pagamento não
    restringe a resolução: a modalidade é fixada na criação da cobrança e
    quem precisa de status final compõe o filtro por conta própria.
    """
    if (
        str(getattr(payment, "provider", "") or "").strip()
        != MERCADO_PAGO_PROVIDER
    ):
        return None

    payload = getattr(payment, "raw_payload", None)
    if not isinstance(payload, dict):
        return None

    raw_installments = payload.get("installments")
    if isinstance(raw_installments, bool) or not isinstance(raw_installments, int):
        return None
    if raw_installments < 1:
        return None

    if raw_installments == 1:
        return PAYMENT_SCENARIO_CASH
    return PAYMENT_SCENARIO_INSTALLMENT


def _money(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(Decimal("0.01"))


def select_order_payment(order) -> Optional[object]:
    """Seleciona o Payment que representa a cobrança aprovada do pedido.

    Regra (read-only, fail-closed):
    - apenas ``provider == mercado_pago``;
    - apenas ``status == approved``;
    - ``amount`` deve ser igual ao ``order.total`` quantizado;
    - entre os elegíveis, o mais recente (``created_at``) vence; sem
      ``created_at``, mantém a ordem de aparição;
    - sem candidato elegível retorna None (o consumidor trata como
      evidência ausente, nunca como custo zero nem como aditivo).
    """
    total = _money(getattr(order, "total", None))
    if total is None:
        return None

    payments = list(getattr(order, "payments", None) or [])
    eligible = []
    for payment in payments:
        if (
            str(getattr(payment, "provider", "") or "").strip()
            != MERCADO_PAGO_PROVIDER
        ):
            continue
        if str(getattr(payment, "status", "") or "").strip().lower() != "approved":
            continue
        amount = _money(getattr(payment, "amount", None))
        if amount is None or amount != total:
            continue
        eligible.append(payment)

    if not eligible:
        return None

    with_timestamp = [
        p for p in eligible if getattr(p, "created_at", None) is not None
    ]
    without_timestamp = [
        p for p in eligible if getattr(p, "created_at", None) is None
    ]
    if with_timestamp:
        with_timestamp.sort(
            key=lambda p: p.created_at,
            reverse=True,
        )
        return with_timestamp[0]
    return without_timestamp[0]


__all__ = [
    "MERCADO_PAGO_PROVIDER",
    "resolve_payment_scenario",
    "select_order_payment",
]
