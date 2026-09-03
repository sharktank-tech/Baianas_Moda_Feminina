"""Snapshot canônico de escrita para o futuro AliExpress placeorder (Fase 8.0.10).

Motivação:
``aliexpress.trade.buy.placeorder`` não pode ler endereço, atributos SKU ou
serviço logístico de tabelas mutáveis no instante da escrita. Este módulo cria
uma camada 1:1 imutável por ``AliExpressOrder`` contendo somente os dados que o
provider de criação poderá consumir.

A tabela NÃO habilita HTTP. O provider real continuará separado e gated.

Regras:
* endereço vem exclusivamente do ``Order`` persistido;
* produto/SKU/quantidade vêm exclusivamente de ``AliExpressOrderItem``;
* ``sku_attr`` é congelado em ``AliExpressOrderItem.supplier_sku_attr`` na
  primeira materialização do fulfillment; este módulo NÃO lê
  ``ProductVariant.attributes`` — usa exclusivamente o frozen value;
* ``logistics_service_name`` é OPCIONAL para o placeorder oficial: quando
  desconhecido é omitido; quando explicitamente fornecido é congelado;
* uma vez materializado, o snapshot não é reconstruído do catálogo;
* PII nunca aparece em repr/log/exception.
"""

from __future__ import annotations

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


def _shipping_address(order) -> dict:
    required = {
        "contact_person": (order.customer_name or "").strip(),
        "mobile_no": (order.customer_phone or "").strip(),
        "zip": (order.zip_code or "").strip(),
        "address": " ".join(
            p for p in [
                (order.street or "").strip(),
                (order.number or "").strip(),
                (order.complement or "").strip(),
                (order.neighborhood or "").strip(),
            ] if p
        ),
        "city": (order.city or "").strip(),
        "province": (order.state or "").strip(),
        "country": "BR",
    }
    if any(not value for value in required.values()):
        raise SubmissionSnapshotError("shipping_address_incomplete")
    return required


def materialize_submission_snapshot(
    ali_order: AliExpressOrder,
    *,
    logistics_service_by_order_item: Optional[Mapping[uuid.UUID, str]] = None,
    session=None,
) -> SubmissionSnapshotResult:
    """Materializa UMA VEZ o payload canônico necessário ao futuro placeorder.

    ``logistics_service_by_order_item`` é OPCIONAL. As docs oficiais de
    ``aliexpress.trade.buy.placeorder`` tratam ``logistics_service_name`` como
    opcional por item:
      * se um valor non-empty for EXPLICITAMENTE fornecido para um item, o
        snapshot inclui ``logistics_service_name`` (congelado);
      * se ausente (ou vazio/whitespace), a chave é OMITIDA do dict — nunca
        string vazia, nunca None, nunca um default inventado.

    O serviço logístico é decisão explícita por item; este helper não consulta
    API de frete nem escolhe automaticamente o serviço mais barato.

    O snapshot é imutável: uma vez criado, novo mapping não altera ``items``.
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
        sku_attr = (item.supplier_sku_attr or "").strip() if getattr(item, "supplier_sku_attr", None) is not None else ""
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
]
