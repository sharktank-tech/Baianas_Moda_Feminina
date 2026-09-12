"""Snapshot canônico de escrita para o futuro AliExpress placeorder.

FASE 8.0.11X endurece o endereço brasileiro sem habilitar HTTP:
* telefone é normalizado para número móvel nacional de 11 dígitos;
* ``phone_country`` é congelado separadamente como ``+55``;
* ``full_name`` é congelado explicitamente;
* ``address`` contém rua + número + bairro;
* ``address2`` preserva somente o complemento quando presente.

FASE 8.0.11Y adiciona revisão OFFLINE e auditável do endereço persistido:
* o snapshot anterior é arquivado integralmente antes da correção;
* itens do pedido nunca são rematerializados nem alterados;
* a correção exige estado ``ready``, zero identidade remota e ledger de
  tentativas completamente reconciliado;
* somente a rejeição exata de endereço conhecida pode entrar nesse fluxo;
* nenhuma função deste módulo faz HTTP ou cria nova tentativa de submissão.

O snapshot ativo continua 1:1 por AliExpressOrder. PII permanece restrita ao
banco e nunca aparece em repr/log/exception.
"""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Mapping, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from loja import db
from loja.modules.models import (
    AliExpressOrder,
    AliExpressRemoteOrder,
    AliExpressSubmissionAttempt,
)
from loja.utils import utcnow

from .sku_attr import build_sku_attr


ADDRESS_VALIDATION_REJECTION_DIAGNOSTIC = (
    "placeorder_remote_error|"
    "remote_error_code=B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"
)
SNAPSHOT_REVISION_REASON_ADDRESS_CORRECTION = "shipping_address_correction"

_CORRECTABLE_SOURCE_FIELDS = frozenset(
    {
        "customer_name",
        "customer_phone",
        "zip_code",
        "street",
        "number",
        "complement",
        "neighborhood",
        "city",
        "state",
    }
)


class SubmissionSnapshotError(Exception):
    """Erro sanitizado de preparação/revisão do placeorder."""


class AliExpressSubmissionSnapshot(db.Model):
    __tablename__ = "aliexpress_submission_snapshots"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aliexpress_order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("aliexpress_orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    shipping_address = db.Column(JSONB, nullable=False)
    items = db.Column(JSONB, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    aliexpress_order = db.relationship(
        "AliExpressOrder",
        backref=db.backref("submission_snapshot", uselist=False),
    )

    def __repr__(self) -> str:
        return f"<AliExpressSubmissionSnapshot order={self.aliexpress_order_id}>"


class AliExpressSubmissionSnapshotRevision(db.Model):
    """Versão arquivada do snapshot antes de uma correção controlada.

    ``shipping_address`` contém PII e é deliberadamente interno. O ``repr`` não
    inclui conteúdo do snapshot, reason livre nem valores alterados.
    """

    __tablename__ = "aliexpress_submission_snapshot_revisions"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aliexpress_order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("aliexpress_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("aliexpress_submission_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revision_no = db.Column(db.Integer, nullable=False)
    shipping_address = db.Column(JSONB, nullable=False)
    items = db.Column(JSONB, nullable=False)
    reason_code = db.Column(
        db.String(64),
        nullable=False,
        default=SNAPSHOT_REVISION_REASON_ADDRESS_CORRECTION,
    )
    changed_fields = db.Column(JSONB, nullable=False)
    snapshot_created_at = db.Column(db.DateTime, nullable=True)
    archived_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "aliexpress_order_id",
            "revision_no",
            name="uq_aliexpress_snapshot_revision_order_no",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AliExpressSubmissionSnapshotRevision "
            f"order={self.aliexpress_order_id} rev={self.revision_no}>"
        )


@dataclass(frozen=True)
class SubmissionSnapshotResult:
    snapshot: AliExpressSubmissionSnapshot
    created: bool


@dataclass(frozen=True)
class SubmissionSnapshotRevisionResult:
    snapshot: AliExpressSubmissionSnapshot
    archived_revision: AliExpressSubmissionSnapshotRevision
    changed_fields: tuple[str, ...]


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_br_mobile(value) -> str:
    """Retorna celular BR nacional ``DD9XXXXXXXX`` ou falha fechado.

    Aceita entrada nacional com 11 dígitos e, por compatibilidade com formulários
    que incluem DDI, 13 dígitos iniciados por ``55``. Não corrige/inventa dígitos.
    """
    digits = _digits(value)
    if len(digits) == 13 and digits.startswith("55"):
        digits = digits[2:]

    if not re.fullmatch(r"[1-9][0-9]9[0-9]{8}", digits):
        raise SubmissionSnapshotError("shipping_mobile_invalid")
    return digits


def _shipping_address(order) -> dict:
    full_name = (order.customer_name or "").strip()
    street = (order.street or "").strip()
    number = (order.number or "").strip()
    complement = (order.complement or "").strip()
    neighborhood = (order.neighborhood or "").strip()

    # Compatibilidade com o contrato histórico: o bairro permanece no endereço
    # principal. O complemento, que pode ser longo, vai para address2.
    address = " ".join(p for p in (street, number, neighborhood) if p)

    required = {
        "contact_person": full_name,
        "full_name": full_name,
        "mobile_no": _normalize_br_mobile(order.customer_phone),
        "phone_country": "+55",
        "zip": (order.zip_code or "").strip(),
        "address": address,
        "city": (order.city or "").strip(),
        "province": (order.state or "").strip().upper(),
        "country": "BR",
    }
    if any(not value for value in required.values()):
        raise SubmissionSnapshotError("shipping_address_incomplete")

    # O AliExpress aceita address2 como extensão. Omitimos quando vazio; nunca
    # inventamos complemento nem o duplicamos no address principal.
    if complement:
        required["address2"] = complement

    return required


def _corrected_source_order(source_order, corrected_fields: Mapping[str, object]):
    """Cria visão efêmera do Order com correções explícitas, sem mutá-lo."""
    if not isinstance(corrected_fields, Mapping) or not corrected_fields:
        raise SubmissionSnapshotError("snapshot_revision_corrections_missing")

    unknown = set(corrected_fields) - _CORRECTABLE_SOURCE_FIELDS
    if unknown:
        raise SubmissionSnapshotError("snapshot_revision_unknown_field")

    values = {}
    for field in _CORRECTABLE_SOURCE_FIELDS:
        value = (
            corrected_fields[field]
            if field in corrected_fields
            else getattr(source_order, field, None)
        )
        if value is not None and not isinstance(value, str):
            raise SubmissionSnapshotError("snapshot_revision_invalid_value")
        values[field] = value

    return SimpleNamespace(**values)


def _snapshot_revision_guard(order: AliExpressOrder, *, session) -> None:
    """Autoriza somente revisão local de rejeição de endereço já reconciliada."""
    if order.state != "ready":
        raise SubmissionSnapshotError("snapshot_revision_state_ineligible")

    if order.ali_order_id:
        raise SubmissionSnapshotError("snapshot_revision_remote_identity_present")

    remote_exists = (
        session.query(AliExpressRemoteOrder.id)
        .filter_by(aliexpress_order_id=order.id)
        .first()
        is not None
    )
    if remote_exists:
        raise SubmissionSnapshotError("snapshot_revision_remote_identity_present")

    unresolved = (
        session.query(AliExpressSubmissionAttempt.id)
        .filter(
            AliExpressSubmissionAttempt.aliexpress_order_id == order.id,
            AliExpressSubmissionAttempt.state.in_(("in_flight", "unknown")),
        )
        .first()
    )
    if unresolved is not None:
        raise SubmissionSnapshotError("snapshot_revision_attempt_unresolved")

    attempt_total = (
        session.query(func.count(AliExpressSubmissionAttempt.id))
        .filter(AliExpressSubmissionAttempt.aliexpress_order_id == order.id)
        .scalar()
        or 0
    )
    if attempt_total != int(order.attempt_count or 0) or attempt_total < 1:
        raise SubmissionSnapshotError("snapshot_revision_attempt_ledger_mismatch")

    latest = (
        session.query(AliExpressSubmissionAttempt)
        .filter_by(aliexpress_order_id=order.id)
        .order_by(
            AliExpressSubmissionAttempt.started_at.desc(),
            AliExpressSubmissionAttempt.created_at.desc(),
        )
        .first()
    )
    if (
        latest is None
        or latest.state != "rejected"
        or latest.last_error != ADDRESS_VALIDATION_REJECTION_DIAGNOSTIC
    ):
        raise SubmissionSnapshotError("snapshot_revision_requires_address_rejection")


def revise_submission_snapshot_shipping(
    ali_order: AliExpressOrder,
    *,
    corrected_fields: Mapping[str, object],
    session=None,
) -> SubmissionSnapshotRevisionResult:
    """Corrige SOMENTE shipping_address com histórico completo e zero HTTP.

    O caller deve fornecer pelo menos uma correção explícita de campo da origem
    (ex.: ``customer_phone``). Os demais dados são lidos do ``Order`` persistido
    e reconstruídos pelo contrato canônico da 8.0.11X. O ``Order`` de origem não
    é alterado; somente o snapshot ativo recebe o novo endereço.

    Antes da alteração, a versão ativa inteira é copiada para
    ``aliexpress_submission_snapshot_revisions``. ``items`` permanece exatamente
    igual no snapshot ativo. A função faz ``flush`` mas não ``commit``.
    """
    session = session or db.session
    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if order is None:
        raise SubmissionSnapshotError("aliexpress_order_missing")

    _snapshot_revision_guard(order, session=session)

    if order.order is None:
        raise SubmissionSnapshotError("source_order_missing")

    snapshot = (
        session.query(AliExpressSubmissionSnapshot)
        .filter_by(aliexpress_order_id=order.id)
        .with_for_update()
        .first()
    )
    if snapshot is None:
        raise SubmissionSnapshotError("snapshot_revision_snapshot_missing")

    corrected_source = _corrected_source_order(order.order, corrected_fields)
    new_shipping = _shipping_address(corrected_source)
    old_shipping = copy.deepcopy(dict(snapshot.shipping_address or {}))
    old_items = copy.deepcopy(list(snapshot.items or []))

    changed_fields = tuple(
        sorted(
            key
            for key in set(old_shipping) | set(new_shipping)
            if old_shipping.get(key) != new_shipping.get(key)
        )
    )
    if not changed_fields:
        raise SubmissionSnapshotError("snapshot_revision_no_changes")

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
        changed_fields=list(changed_fields),
        snapshot_created_at=snapshot.created_at,
    )
    session.add(archived)

    # Única mutação funcional da revisão. O snapshot id, created_at e items são
    # preservados; estado/attempt_count/attempt_key da ordem não são tocados.
    snapshot.shipping_address = new_shipping
    session.flush()

    return SubmissionSnapshotRevisionResult(
        snapshot=snapshot,
        archived_revision=archived,
        changed_fields=changed_fields,
    )


def materialize_submission_snapshot(
    ali_order: AliExpressOrder,
    *,
    logistics_service_by_order_item: Optional[Mapping[uuid.UUID, str]] = None,
    session=None,
) -> SubmissionSnapshotResult:
    """Materializa UMA VEZ o payload canônico necessário ao placeorder.

    ``logistics_service_by_order_item`` é opcional. O snapshot é imutável no
    fluxo normal: uma vez criado, mudanças posteriores no pedido ou catálogo não
    o reescrevem. A única exceção é a revisão auditada e explicitamente guardada
    de ``revise_submission_snapshot_shipping``.
    """
    session = session or db.session
    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if order is None:
        raise SubmissionSnapshotError("aliexpress_order_missing")

    existing = session.query(AliExpressSubmissionSnapshot).filter_by(
        aliexpress_order_id=order.id
    ).first()
    if existing is not None:
        return SubmissionSnapshotResult(snapshot=existing, created=False)

    if order.state not in ("ready", "submitting"):
        raise SubmissionSnapshotError("submission_snapshot_state_ineligible")
    if order.order is None:
        raise SubmissionSnapshotError("source_order_missing")

    shipping = _shipping_address(order.order)
    logistics_map = logistics_service_by_order_item or {}
    frozen_items = []
    for item in list(order.items):
        if not item.supplier_product_external_id or not item.supplier_sku_external_id:
            raise SubmissionSnapshotError("submission_item_identity_incomplete")
        sku_attr = (
            (item.supplier_sku_attr or "").strip()
            if getattr(item, "supplier_sku_attr", None) is not None
            else ""
        )
        if not sku_attr:
            raise SubmissionSnapshotError("submission_sku_attr_missing")
        service = str(logistics_map.get(item.order_item_id) or "").strip()
        entry = {
            "order_item_id": str(item.order_item_id),
            "product_id": str(item.supplier_product_external_id),
            "sku_attr": sku_attr,
            "product_count": int(item.quantity),
        }
        if service:
            entry["logistics_service_name"] = service
        frozen_items.append(entry)

    if not frozen_items:
        raise SubmissionSnapshotError("submission_items_missing")

    snapshot = AliExpressSubmissionSnapshot(
        aliexpress_order_id=order.id,
        shipping_address=shipping,
        items=frozen_items,
    )
    session.add(snapshot)
    session.flush()
    return SubmissionSnapshotResult(snapshot=snapshot, created=True)


__all__ = [
    "ADDRESS_VALIDATION_REJECTION_DIAGNOSTIC",
    "SNAPSHOT_REVISION_REASON_ADDRESS_CORRECTION",
    "AliExpressSubmissionSnapshot",
    "AliExpressSubmissionSnapshotRevision",
    "SubmissionSnapshotError",
    "SubmissionSnapshotResult",
    "SubmissionSnapshotRevisionResult",
    "build_sku_attr",
    "materialize_submission_snapshot",
    "revise_submission_snapshot_shipping",
    "_normalize_br_mobile",
    "_shipping_address",
]
