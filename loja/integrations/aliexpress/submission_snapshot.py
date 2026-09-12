"""Snapshot canônico de escrita para o futuro AliExpress placeorder.

FASE 8.0.11X endurece o endereço brasileiro sem habilitar HTTP:
* telefone é normalizado para número móvel nacional de 11 dígitos;
* ``phone_country`` é congelado separadamente como ``+55``;
* ``full_name`` é congelado explicitamente;
* ``address`` contém rua + número + bairro;
* ``address2`` preserva somente o complemento quando presente.

O snapshot continua imutável depois de materializado e PII nunca aparece em
repr/log/exception.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional

from sqlalchemy.dialects.postgresql import JSONB, UUID

from loja import db
from loja.modules.models import AliExpressOrder
from loja.utils import utcnow

from .sku_attr import build_sku_attr


class SubmissionSnapshotError(Exception):
    """Erro sanitizado de preparação do placeorder."""


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


@dataclass(frozen=True)
class SubmissionSnapshotResult:
    snapshot: AliExpressSubmissionSnapshot
    created: bool


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


def materialize_submission_snapshot(
    ali_order: AliExpressOrder,
    *,
    logistics_service_by_order_item: Optional[Mapping[uuid.UUID, str]] = None,
    session=None,
) -> SubmissionSnapshotResult:
    """Materializa UMA VEZ o payload canônico necessário ao placeorder.

    ``logistics_service_by_order_item`` é opcional. O snapshot é imutável: uma
    vez criado, mudanças posteriores no pedido ou catálogo não o reescrevem.
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
    "AliExpressSubmissionSnapshot",
    "SubmissionSnapshotError",
    "SubmissionSnapshotResult",
    "build_sku_attr",
    "materialize_submission_snapshot",
    "_normalize_br_mobile",
    "_shipping_address",
]
