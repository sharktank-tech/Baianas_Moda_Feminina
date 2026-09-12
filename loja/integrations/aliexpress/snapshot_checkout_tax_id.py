"""FASE 8.0.12C — congela CPF do Order em snapshots AliExpress novos.

O listener atua somente em ``AliExpressSubmissionSnapshot`` ainda não
persistidos. Pedidos/snapshots legados não são reescritos. Ausência ou CPF
inválido falha fechado antes do snapshot ser gravado e sem ecoar PII.
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

    try:
        cpf = normalize_br_cpf(getattr(ali_order.order, "customer_cpf", None))
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


__all__ = ["_freeze_checkout_cpf"]
