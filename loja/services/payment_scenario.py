"""Resolvedor read-only de modalidade de pagamento a partir do Payment persistido.

Extrai exclusivamente do payload Mercado Pago persistido a contagem de
parcelas, quando presente e coerente, e devolve o cenário de unit economics
correspondente ("cash" ou "installment"). Não consulta provider, não muta
modelos e nunca infere modalidade sem evidência: ausência ou valor inválido
retornam None (desconhecido).

Evidência única aceita: ``raw_payload["installments"]`` como inteiro >= 1.
1 = à vista; >= 2 = parcelada. ``payment_type_id`` não é usado porque não
distingue crédito de 1x de débito nem comprova parcelamento sem a contagem.
"""

from __future__ import annotations

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


__all__ = [
    "MERCADO_PAGO_PROVIDER",
    "resolve_payment_scenario",
]
