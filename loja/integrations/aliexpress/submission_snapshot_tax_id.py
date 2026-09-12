"""FASE 8.0.12A — correção OFFLINE e auditável de CPF no snapshot AliExpress.

Nenhuma função deste módulo faz HTTP, cria SubmissionAttempt, altera
``attempt_count`` ou cria slot de retry. O CPF é aceito SOMENTE de forma
explícita pelo caller, validado localmente e persistido apenas no
``shipping_address`` do snapshot de submissão.
"""

from __future__ import annotations

import copy
import re

from sqlalchemy import func

from loja import db
from .submission_snapshot import (
    AliExpressSubmissionSnapshot,
    AliExpressSubmissionSnapshotRevision,
    SNAPSHOT_REVISION_REASON_ADDRESS_CORRECTION,
    SubmissionSnapshotError,
    SubmissionSnapshotRevisionResult,
    _snapshot_revision_guard,
)


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_br_cpf(value) -> str:
    """Normaliza CPF para 11 dígitos e valida os dois dígitos verificadores.

    Não gera, completa ou corrige CPF. Sequências repetidas são inválidas.
    Exceções contêm apenas código sanitizado, nunca o documento recebido.
    """
    digits = _digits(value)
    if len(digits) != 11 or len(set(digits)) == 1:
        raise SubmissionSnapshotError("shipping_cpf_invalid")

    numbers = [int(ch) for ch in digits]

    total = sum(numbers[i] * (10 - i) for i in range(9))
    check1 = (total * 10) % 11
    if check1 == 10:
        check1 = 0

    total = sum(numbers[i] * (11 - i) for i in range(10))
    check2 = (total * 10) % 11
    if check2 == 10:
        check2 = 0

    if numbers[9] != check1 or numbers[10] != check2:
        raise SubmissionSnapshotError("shipping_cpf_invalid")

    return digits


def revise_submission_snapshot_cpf(
    ali_order,
    *,
    cpf,
    session=None,
) -> SubmissionSnapshotRevisionResult:
    """Adiciona/corrige CPF no snapshot ativo com histórico completo.

    Guards reutilizados da FASE 8.0.11Y:
    - ordem exatamente ``ready``;
    - zero identidade remota;
    - zero attempts ``unknown``/``in_flight``;
    - ledger total igual a ``attempt_count``;
    - latest attempt ``rejected`` com diagnóstico exato de endereço.

    Antes da mutação, o snapshot ativo completo é arquivado como nova revisão.
    ``items``, id/created_at do snapshot, Order e attempts permanecem intactos.
    Faz ``flush`` e NÃO faz ``commit``.
    """
    session = session or db.session
    normalized_cpf = _normalize_br_cpf(cpf)

    order = (
        session.query(type(ali_order))
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if order is None:
        raise SubmissionSnapshotError("aliexpress_order_missing")

    _snapshot_revision_guard(order, session=session)

    snapshot = (
        session.query(AliExpressSubmissionSnapshot)
        .filter_by(aliexpress_order_id=order.id)
        .with_for_update()
        .first()
    )
    if snapshot is None:
        raise SubmissionSnapshotError("snapshot_revision_snapshot_missing")

    old_shipping = copy.deepcopy(dict(snapshot.shipping_address or {}))
    old_items = copy.deepcopy(list(snapshot.items or []))

    if old_shipping.get("cpf") == normalized_cpf:
        raise SubmissionSnapshotError("snapshot_revision_no_changes")

    new_shipping = copy.deepcopy(old_shipping)
    new_shipping["cpf"] = normalized_cpf

    latest_revision_no = (
        session.query(func.max(AliExpressSubmissionSnapshotRevision.revision_no))
        .filter_by(aliexpress_order_id=order.id)
        .scalar()
        or 0
    )
    archived = AliExpressSubmissionSnapshotRevision(
        aliexpress_order_id=order.id,
        snapshot_id=snapshot.id,
        revision_no=int(latest_revision_no) + 1,
        shipping_address=old_shipping,
        items=old_items,
        reason_code=SNAPSHOT_REVISION_REASON_ADDRESS_CORRECTION,
        changed_fields=["cpf"],
        snapshot_created_at=snapshot.created_at,
    )
    session.add(archived)

    snapshot.shipping_address = new_shipping
    session.flush()

    return SubmissionSnapshotRevisionResult(
        snapshot=snapshot,
        archived_revision=archived,
        changed_fields=("cpf",),
    )


__all__ = ["_normalize_br_cpf", "revise_submission_snapshot_cpf"]
