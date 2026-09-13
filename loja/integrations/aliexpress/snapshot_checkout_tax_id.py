"""FASE 8.0.12C — congela CPF do Order em snapshots AliExpress novos.

O listener atua somente em ``AliExpressSubmissionSnapshot`` ainda não
persistidos. Pedidos/snapshots legados sem CPF continuam compatíveis. Quando o
Order possui CPF, o valor precisa ser válido e é congelado no shipping_address;
valor inválido falha fechado sem ecoar PII.

A obrigatoriedade para NOVOS checkouts AliExpress fica no guard pré-Mercado
Pago. Assim histórico/fixtures legados não são reescritos nem invalidados.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from loja.modules.models import AliExpressOrder
from loja.utils.br_tax_id import BrazilianTaxIdError, normalize_br_cpf

from .submission_snapshot import AliExpressSubmissionSnapshot, SubmissionSnapshotError


def _freeze_checkout_cpf(session, snapshot: AliExpressSubmissionSnapshot) -> None:
    ali_order = (
        session.query(AliExpressOrder)
        .filter_by(id=snapshot.aliexpress_order_id)
        .first()
    )
    if ali_order is None or ali_order.order is None:
        raise SubmissionSnapshotError("source_order_missing")

    raw_cpf = getattr(ali_order.order, "customer_cpf", None)
    if raw_cpf is None or not str(raw_cpf).strip():
        # Compatibilidade deliberada com pedidos/snapshots legados. Novos
        # checkouts AliExpress são bloqueados antes do Mercado Pago pelo guard
        # em payment_models.py, portanto não chegam aqui sem CPF.
        return

    try:
        cpf = normalize_br_cpf(raw_cpf)
    except BrazilianTaxIdError as error:
        raise SubmissionSnapshotError("shipping_cpf_invalid") from error

    shipping = dict(snapshot.shipping_address or {})
    shipping["cpf"] = cpf
    snapshot.shipping_address = shipping


@event.listens_for(Session, "before_flush")
def _guard_new_submission_snapshot_cpf(session, flush_context, instances):
    for obj in tuple(session.new):
        if not isinstance(obj, AliExpressSubmissionSnapshot):
            continue
        _freeze_checkout_cpf(session, obj)


# Ponto tardio de bootstrap da integração: registra o hook pós-pagamento sem
# acoplar o provider à transação financeira. O próprio hook permanece hard-off
# até ALIEXPRESS_POSTPAYMENT_AUTODISPATCH_ENABLED ser exatamente "1".
from . import postpayment_autodispatch as _postpayment_autodispatch  # noqa: F401,E402


__all__ = ["_freeze_checkout_cpf"]