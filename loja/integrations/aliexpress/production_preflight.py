"""FASE 8.1.0 — preflight operacional de fulfillment AliExpress.

Esta camada é deliberadamente READ-ONLY: não transiciona estado, não cria
``SubmissionAttempt``, não seleciona provider e não executa HTTP. Ela responde
somente se uma ``AliExpressOrder`` já materializada está operacionalmente apta
para o futuro orquestrador de produção.

Condições mínimas:
- pedido de origem continua ``paid``;
- AliExpressOrder está exatamente ``ready``;
- supplier atribuído e itens íntegros/credencial aprovados pelo gate existente;
- zero identidade remota;
- zero attempt não reconciliado;
- ledger de attempts consistente com ``attempt_count``;
- nenhum attempt confirmado em uma ordem que voltou a ``ready``;
- submission snapshot estruturalmente válido;
- shipping BR do snapshot contém telefone/CEP/UF/CPF canônicos.

Nenhum código de erro contém PII, token, attempt key ou remote order id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func

from loja import db
from loja.modules.models import (
    AliExpressOrder,
    AliExpressRemoteOrder,
    AliExpressSubmissionAttempt,
)
from loja.utils.br_tax_id import BrazilianTaxIdError, normalize_br_cpf

from .submission import _submission_gate, _validate_submission_snapshot
from .submission_snapshot import (
    AliExpressSubmissionSnapshot,
    SubmissionSnapshotError,
    _normalize_br_mobile,
)


PREFLIGHT_ORDER_MISSING = "production_preflight_order_missing"
PREFLIGHT_SOURCE_NOT_PAID = "production_preflight_source_not_paid"
PREFLIGHT_STATE_NOT_READY = "production_preflight_state_not_ready"
PREFLIGHT_REMOTE_IDENTITY = "production_preflight_remote_identity_present"
PREFLIGHT_ATTEMPT_UNRESOLVED = "production_preflight_attempt_unresolved"
PREFLIGHT_LEDGER_MISMATCH = "production_preflight_attempt_ledger_mismatch"
PREFLIGHT_CONFIRMED_HISTORY = "production_preflight_confirmed_attempt_present"
PREFLIGHT_SHIPPING_INVALID = "production_preflight_shipping_invalid"

_REQUIRED_SHIPPING_FIELDS = (
    "contact_person",
    "full_name",
    "mobile_no",
    "phone_country",
    "zip",
    "address",
    "city",
    "province",
    "country",
    "cpf",
)


@dataclass(frozen=True)
class ProductionSubmissionEligibility:
    eligible: bool
    code: Optional[str]
    attempt_count: int
    attempt_rows: int
    snapshot_present: bool


def _result(
    *,
    eligible: bool,
    code: Optional[str],
    attempt_count: int = 0,
    attempt_rows: int = 0,
    snapshot_present: bool = False,
) -> ProductionSubmissionEligibility:
    return ProductionSubmissionEligibility(
        eligible=eligible,
        code=code,
        attempt_count=int(attempt_count or 0),
        attempt_rows=int(attempt_rows or 0),
        snapshot_present=bool(snapshot_present),
    )


def _validate_production_shipping(snapshot: AliExpressSubmissionSnapshot) -> Optional[str]:
    shipping = getattr(snapshot, "shipping_address", None)
    if not isinstance(shipping, dict):
        return PREFLIGHT_SHIPPING_INVALID

    for field in _REQUIRED_SHIPPING_FIELDS:
        value = shipping.get(field)
        if value is None or not str(value).strip():
            return PREFLIGHT_SHIPPING_INVALID

    if str(shipping.get("country")).strip().upper() != "BR":
        return PREFLIGHT_SHIPPING_INVALID
    if str(shipping.get("phone_country")).strip() != "+55":
        return PREFLIGHT_SHIPPING_INVALID
    if not re.fullmatch(r"[0-9]{8}", str(shipping.get("zip")).strip()):
        return PREFLIGHT_SHIPPING_INVALID
    if not re.fullmatch(r"[A-Z]{2}", str(shipping.get("province")).strip()):
        return PREFLIGHT_SHIPPING_INVALID

    raw_mobile = str(shipping.get("mobile_no")).strip()
    try:
        if _normalize_br_mobile(raw_mobile) != raw_mobile:
            return PREFLIGHT_SHIPPING_INVALID
    except SubmissionSnapshotError:
        return PREFLIGHT_SHIPPING_INVALID

    raw_cpf = str(shipping.get("cpf")).strip()
    try:
        if normalize_br_cpf(raw_cpf) != raw_cpf:
            return PREFLIGHT_SHIPPING_INVALID
    except BrazilianTaxIdError:
        return PREFLIGHT_SHIPPING_INVALID

    return None


def evaluate_production_submission_eligibility(
    ali_order: AliExpressOrder,
    *,
    session=None,
    refresher=None,
) -> ProductionSubmissionEligibility:
    """Avalia elegibilidade sem mutar a ordem e sem tocar no provider."""
    session = session or db.session

    order = (
        session.query(AliExpressOrder)
        .filter_by(id=getattr(ali_order, "id", None))
        .populate_existing()
        .first()
    )
    if order is None:
        return _result(eligible=False, code=PREFLIGHT_ORDER_MISSING)

    attempt_count = int(order.attempt_count or 0)

    if order.order is None or order.order.status != "paid":
        return _result(
            eligible=False,
            code=PREFLIGHT_SOURCE_NOT_PAID,
            attempt_count=attempt_count,
        )

    if order.state != "ready":
        return _result(
            eligible=False,
            code=PREFLIGHT_STATE_NOT_READY,
            attempt_count=attempt_count,
        )

    if order.ali_order_id:
        return _result(
            eligible=False,
            code=PREFLIGHT_REMOTE_IDENTITY,
            attempt_count=attempt_count,
        )

    if (
        session.query(AliExpressRemoteOrder.id)
        .filter_by(aliexpress_order_id=order.id)
        .first()
        is not None
    ):
        return _result(
            eligible=False,
            code=PREFLIGHT_REMOTE_IDENTITY,
            attempt_count=attempt_count,
        )

    unresolved = (
        session.query(AliExpressSubmissionAttempt.id)
        .filter(
            AliExpressSubmissionAttempt.aliexpress_order_id == order.id,
            AliExpressSubmissionAttempt.state.in_(("in_flight", "unknown")),
            AliExpressSubmissionAttempt.completed_at.is_(None),
        )
        .first()
    )
    if unresolved is not None:
        return _result(
            eligible=False,
            code=PREFLIGHT_ATTEMPT_UNRESOLVED,
            attempt_count=attempt_count,
        )

    attempt_rows = (
        session.query(func.count(AliExpressSubmissionAttempt.id))
        .filter(AliExpressSubmissionAttempt.aliexpress_order_id == order.id)
        .scalar()
        or 0
    )
    if int(attempt_rows) != attempt_count:
        return _result(
            eligible=False,
            code=PREFLIGHT_LEDGER_MISMATCH,
            attempt_count=attempt_count,
            attempt_rows=attempt_rows,
        )

    confirmed_exists = (
        session.query(AliExpressSubmissionAttempt.id)
        .filter_by(aliexpress_order_id=order.id, state="confirmed")
        .first()
        is not None
    )
    if confirmed_exists:
        return _result(
            eligible=False,
            code=PREFLIGHT_CONFIRMED_HISTORY,
            attempt_count=attempt_count,
            attempt_rows=attempt_rows,
        )

    reason = _submission_gate(order, list(order.items), refresher=refresher)
    if reason is not None:
        return _result(
            eligible=False,
            code=reason,
            attempt_count=attempt_count,
            attempt_rows=attempt_rows,
        )

    snapshot = (
        session.query(AliExpressSubmissionSnapshot)
        .filter_by(aliexpress_order_id=order.id)
        .first()
    )
    if snapshot is None:
        return _result(
            eligible=False,
            code="submission_snapshot_missing",
            attempt_count=attempt_count,
            attempt_rows=attempt_rows,
            snapshot_present=False,
        )

    structural_error = _validate_submission_snapshot(snapshot)
    if structural_error is not None:
        return _result(
            eligible=False,
            code=structural_error,
            attempt_count=attempt_count,
            attempt_rows=attempt_rows,
            snapshot_present=True,
        )

    shipping_error = _validate_production_shipping(snapshot)
    if shipping_error is not None:
        return _result(
            eligible=False,
            code=shipping_error,
            attempt_count=attempt_count,
            attempt_rows=attempt_rows,
            snapshot_present=True,
        )

    return _result(
        eligible=True,
        code=None,
        attempt_count=attempt_count,
        attempt_rows=attempt_rows,
        snapshot_present=True,
    )


__all__ = [
    "ProductionSubmissionEligibility",
    "evaluate_production_submission_eligibility",
    "PREFLIGHT_ORDER_MISSING",
    "PREFLIGHT_SOURCE_NOT_PAID",
    "PREFLIGHT_STATE_NOT_READY",
    "PREFLIGHT_REMOTE_IDENTITY",
    "PREFLIGHT_ATTEMPT_UNRESOLVED",
    "PREFLIGHT_LEDGER_MISMATCH",
    "PREFLIGHT_CONFIRMED_HISTORY",
    "PREFLIGHT_SHIPPING_INVALID",
]
