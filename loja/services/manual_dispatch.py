"""Registro manual de compra no fornecedor e despacho (sem automação).

A interface registra compras JÁ realizadas pelo operador: salvar nunca
compra no AliExpress, consulta o fornecedor ou habilita automação. Por isso
NÃO reutiliza `AliExpressOrder` (máquina do fulfillment automático) e NÃO
preenche seus estados — usa tabelas próprias (`manual_supplier_purchases`,
`shipment_items`, `manual_operational_audit`; ver migration 0039).

Reutiliza: `Order`/`OrderItem` (quantidades vendidas, vínculos por UUID),
`payment_is_confirmed` (prova de pagamento), `Shipment`/`TrackingEvent`
(remessa com `tracking_code` único) e as regras de normalização de
`TrackingService` — mas NÃO seu `commit` interno (aqui só `flush()`; quem
chama commita, mantendo a transação única e atômica).

Estados de pedido aceitos para registro: ``ELIGIBLE_ORDER_STATUSES`` +
pagamento comprovado. Bloqueados: cancelados, reembolsados, contestados,
em revisão e qualquer outro estado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loja import db
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.modules.models import (
    ManualOperationalAudit,
    ManualSupplierPurchase,
    ManualSupplierPurchaseItem,
    Shipment,
    ShipmentItem,
    TrackingEvent,
)
from loja.services.order_payment_evidence import payment_is_confirmed
from loja.utils import utcnow

#: Único estado operacional elegível + pagamento comprovado.
ELIGIBLE_ORDER_STATUSES = frozenset({"paid"})

#: Estados explicitamente recusados (mensagem própria no serviço).
BLOCKED_ORDER_STATUSES = frozenset({
    "cancelled", "refunded", "charged_back", "manual_review",
    "pending_payment", "payment_failed", "expired",
})

TRACKING_CODE_MAX = 64
EVIDENCE_REF_MAX = 255
NOTE_MAX = 2000


@dataclass(frozen=True)
class ManualDispatchResult:
    created: bool
    purchase_id: object = None
    shipment_id: object = None


def _norm_code(value, *, field, maximum=TRACKING_CODE_MAX) -> str:
    text = str(value or "").strip().upper()
    if not (3 <= len(text) <= maximum):
        raise ValueError(f"{field}_invalid")
    if any(not (ch.isalnum() or ch in " ._-") for ch in text):
        raise ValueError(f"{field}_invalid")
    return text


def _require_text(value, *, field, maximum) -> str:
    text = str(value or "").strip()
    if not (1 <= len(text) <= maximum):
        raise ValueError(f"{field}_invalid")
    return text


def _require_qty(value) -> int:
    try:
        qty = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("quantity_invalid") from None
    if qty <= 0:
        raise ValueError("quantity_invalid")
    return qty


def _new_bigint_id() -> int:
    """ID BigInteger sem coordenação entre dialetos/processos.

    `shipments.id` tem sequence no PostgreSQL, mas o SQLite dos testes não
    gera chaves BigInteger sozinho — e `max()+1` teria corrida real entre
    processos. 63 bits aleatórios dispensam lock/sequência com colisão
    desprezível nesta escala; nunca colidem com a sequence (faixa baixa).
    """
    return (uuid.uuid4().int & ((1 << 63) - 1)) or 1


def _parse_naive_dt(value, *, field) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field}_invalid") from None
    return parsed.replace(tzinfo=None)


def _require_tz(value) -> str:
    name = str(value or "").strip() or "America/Sao_Paulo"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("purchased_tz_invalid") from None
    return name


def _locked_order(order_id) -> Order:
    try:
        wanted = uuid.UUID(str(order_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("order_invalid") from None
    # `of=Order`: Order tem relacionamentos lazy="joined" (user, coupon);
    # o FOR UPDATE irrestrito é recusado no PostgreSQL. Mesmo padrão do
    # caminho de liberação de reservas.
    order = (
        Order.query.filter_by(id=wanted)
        .with_for_update(of=Order)
        .populate_existing()
        .first()
    )
    if order is None:
        raise ValueError("order_not_found")
    return order


def _require_eligible(order: Order) -> None:
    """Pedido com pagamento comprovado e estado operacional elegível."""
    if order.status in BLOCKED_ORDER_STATUSES or order.status not in ELIGIBLE_ORDER_STATUSES:
        raise ValueError("order_status_not_eligible")
    if not payment_is_confirmed(order):
        raise ValueError("payment_not_confirmed")


def _order_items_map(order: Order) -> dict:
    return {item.id: item for item in (order.items or [])}


def _sum_registered(order_item_id, *, exclude_purchase_id=None) -> int:
    query = db.session.query(
        db.func.coalesce(db.func.sum(ManualSupplierPurchaseItem.quantity), 0)
    ).join(
        ManualSupplierPurchase,
        ManualSupplierPurchase.id == ManualSupplierPurchaseItem.purchase_id,
    ).filter(ManualSupplierPurchaseItem.order_item_id == order_item_id)
    if exclude_purchase_id is not None:
        query = query.filter(ManualSupplierPurchase.id != exclude_purchase_id)
    return int(query.scalar() or 0)


def _audit(*, entity, entity_id, field, old, new, admin_user_id, reason) -> None:
    db.session.add(ManualOperationalAudit(
        id=uuid.uuid4(),
        entity=entity,
        entity_id=str(entity_id),
        field=field,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
        admin_user_id=admin_user_id,
        reason=_require_text(reason, field="reason", maximum=500),
    ))


def _check_fresh(row, expected_updated_at) -> None:
    """Versão obrigatória, comparada exatamente sob lock.

    O token viaja com microssegundos (`isoformat` completo); omitir ou
    adulterar o token recusa a alteração — peça recarregar.
    """
    if expected_updated_at in (None, ""):
        raise ValueError("expected_updated_at_required")
    expected = _parse_naive_dt(expected_updated_at, field="expected_updated_at")
    expected = expected.replace(tzinfo=None)
    current = getattr(row, "updated_at", None)
    current = current.replace(tzinfo=None) if current is not None else None
    if current is None or current != expected:
        raise ValueError("stale_state_reload")


def register_manual_purchase(
    *,
    order_id,
    external_order_id,
    items: Mapping,
    purchased_at,
    purchased_tz=None,
    evidence_ref,
    note=None,
    admin_user_id=None,
) -> ManualDispatchResult:
    """Registra compra externa já realizada (idempotente por número).

    Mesmo `external_order_id` no MESMO pedido → devolve o registro existente
    sem duplicar. Mesmo número em OUTRO pedido → `ValueError`. Nunca altera
    pagamento, preço, estoque ou reservas: só insere compra + itens.
    """
    order = _locked_order(order_id)
    _require_eligible(order)
    code = _norm_code(external_order_id, field="external_order_id")

    existing = ManualSupplierPurchase.query.filter_by(
        external_order_id=code
    ).first()
    if existing is not None:
        if existing.order_id != order.id:
            raise ValueError("external_order_id_in_use")
        return ManualDispatchResult(created=False, purchase_id=existing.id)

    if not items:
        raise ValueError("items_required")
    sold = _order_items_map(order)
    if not sold:
        raise ValueError("order_without_items")
    links = {}
    for raw_item_id, raw_qty in dict(items or {}).items():
        try:
            item_id = uuid.UUID(str(raw_item_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("order_item_invalid") from None
        item = sold.get(item_id)
        if item is None:
            raise ValueError("order_item_not_in_order")
        qty = _require_qty(raw_qty)
        if _sum_registered(item_id) + qty > int(item.quantity or 0):
            raise ValueError("quantity_exceeds_sold")
        links[item_id] = qty

    purchase = ManualSupplierPurchase(
        id=uuid.uuid4(),
        order_id=order.id,
        external_order_id=code,
        purchased_at=_parse_naive_dt(purchased_at, field="purchased_at"),
        purchased_tz=_require_tz(purchased_tz),
        evidence_ref=_require_text(
            evidence_ref, field="evidence_ref", maximum=EVIDENCE_REF_MAX
        ),
        note=(str(note).strip()[:NOTE_MAX] if note else None),
        created_by=admin_user_id,
    )
    db.session.add(purchase)
    db.session.flush()
    for item_id, qty in links.items():
        db.session.add(ManualSupplierPurchaseItem(
            id=uuid.uuid4(),
            purchase_id=purchase.id,
            order_item_id=item_id,
            quantity=qty,
        ))
    _audit(
        entity="purchase", entity_id=purchase.id, field="created",
        old=None, new=f"{code} | evidencia: {purchase.evidence_ref}",
        admin_user_id=admin_user_id, reason="registro inicial",
    )
    db.session.flush()
    return ManualDispatchResult(created=True, purchase_id=purchase.id)


def _sum_covered(purchase_id, order_item_id, *, exclude_shipment_id=None) -> int:
    query = db.session.query(
        db.func.coalesce(db.func.sum(ShipmentItem.quantity), 0)
    ).join(
        Shipment, Shipment.id == ShipmentItem.shipment_id
    ).filter(
        Shipment.manual_purchase_id == purchase_id,
        ShipmentItem.order_item_id == order_item_id,
    )
    if exclude_shipment_id is not None:
        query = query.filter(Shipment.id != exclude_shipment_id)
    return int(query.scalar() or 0)


def _purchase_linked_qty(purchase, order_item_id) -> int:
    for link in (purchase.items or []):
        if link.order_item_id == order_item_id:
            return int(link.quantity or 0)
    return 0


def register_manual_shipment(
    *,
    order_id,
    purchase_id,
    tracking_code,
    carrier,
    posted_at=None,
    estimated_delivery=None,
    evidence_ref,
    items: Mapping,
    admin_user_id=None,
) -> ManualDispatchResult:
    """Registra remessa vinculada à compra (idempotente por código).

    Status inicial sempre `aguardando_postagem` + primeiro evento; postagem,
    trânsito e entrega exigem registros próprios (fora deste serviço).
    Mesmas regras de `TrackingService` sem seu `commit` interno.
    """
    order = _locked_order(order_id)
    _require_eligible(order)
    try:
        wanted_purchase = uuid.UUID(str(purchase_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("purchase_invalid") from None
    purchase = db.session.query(ManualSupplierPurchase).filter_by(
        id=wanted_purchase, order_id=order.id
    ).with_for_update(of=ManualSupplierPurchase).first()
    if purchase is None:
        raise ValueError("purchase_not_in_order")

    code = _norm_code(tracking_code, field="tracking_code")
    carrier_name = _require_text(carrier, field="carrier", maximum=60)
    ref = _require_text(
        evidence_ref, field="evidence_ref", maximum=EVIDENCE_REF_MAX
    )

    if not items:
        raise ValueError("items_required")
    sold = _order_items_map(order)
    coverage = {}
    for raw_item_id, raw_qty in dict(items or {}).items():
        try:
            item_id = uuid.UUID(str(raw_item_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("order_item_invalid") from None
        if item_id not in sold:
            raise ValueError("order_item_not_in_order")
        qty = _require_qty(raw_qty)
        if _purchase_linked_qty(purchase, item_id) <= 0:
            raise ValueError("order_item_not_in_purchase")
        coverage[item_id] = qty

    existing = Shipment.query.filter_by(tracking_code=code).first()
    if existing is not None:
        if existing.order_id != order.id:
            raise ValueError("tracking_code_in_use")
        if existing.manual_purchase_id != purchase.id:
            # Mesmo código em OUTRA compra (ainda que do mesmo pedido):
            # vínculo diferente = conflito claro, sem escrita parcial.
            raise ValueError("tracking_code_other_purchase")
        covered_now = {
            str(link.order_item_id): int(link.quantity or 0)
            for link in ShipmentItem.query.filter_by(
                shipment_id=existing.id
            ).all()
        }
        wanted = {str(item_id): qty for item_id, qty in coverage.items()}
        if covered_now != wanted:
            raise ValueError("shipment_content_differs")
        return ManualDispatchResult(
            created=False, purchase_id=purchase.id, shipment_id=existing.id
        )

    for item_id, qty in coverage.items():
        linked = _purchase_linked_qty(purchase, item_id)
        if _sum_covered(purchase.id, item_id) + qty > linked:
            raise ValueError("quantity_exceeds_purchase")

    shipment = Shipment(
        id=_new_bigint_id(),
        order_id=order.id,
        manual_purchase_id=purchase.id,
        tracking_code=code,
        carrier=carrier_name,
        provider="manual",
        status="aguardando_postagem",
        estimated_delivery=(
            _parse_naive_dt(estimated_delivery, field="estimated_delivery").date()
            if estimated_delivery else None
        ),
    )
    db.session.add(shipment)
    db.session.flush()
    db.session.add(TrackingEvent(
        id=_new_bigint_id(),
        shipment_id=shipment.id,
        status="aguardando_postagem",
        # Descrição pública genérica: a referência da postagem é privada
        # (auditoria abaixo) e nunca aparece nas telas do cliente.
        description="Código de rastreio cadastrado. Aguardando postagem.",
        event_at=utcnow(),
    ))
    for item_id, qty in coverage.items():
        db.session.add(ShipmentItem(
            shipment_id=shipment.id,
            order_item_id=item_id,
            quantity=qty,
        ))
    if posted_at:
        db.session.add(TrackingEvent(
            id=_new_bigint_id(),
            shipment_id=shipment.id,
            status="postado",
            description="Postagem registrada.",
            event_at=_parse_naive_dt(posted_at, field="posted_at"),
        ))
        shipment.status = "postado"
    _audit(
        entity="shipment", entity_id=shipment.id, field="created",
        old=None,
        new=f"{code} | {carrier_name} | evidencia: {ref}",
        admin_user_id=admin_user_id, reason="registro inicial",
    )
    db.session.flush()
    return ManualDispatchResult(
        created=True, purchase_id=purchase.id, shipment_id=shipment.id
    )


def correct_manual_purchase(
    *,
    purchase_id,
    external_order_id,
    reason,
    admin_user_id=None,
    expected_updated_at=None,
) -> ManualSupplierPurchase:
    """Corrige o número externo (auditoria antes/depois, sem efeitos)."""
    try:
        wanted = uuid.UUID(str(purchase_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("purchase_invalid") from None
    purchase = db.session.query(ManualSupplierPurchase).filter_by(
        id=wanted
    ).with_for_update(of=ManualSupplierPurchase).populate_existing().first()
    if purchase is None:
        raise ValueError("purchase_not_found")
    _check_fresh(purchase, expected_updated_at)
    code = _norm_code(external_order_id, field="external_order_id")
    if code == purchase.external_order_id:
        return purchase
    clash = ManualSupplierPurchase.query.filter_by(
        external_order_id=code
    ).first()
    if clash is not None and clash.id != purchase.id:
        raise ValueError("external_order_id_in_use")
    old = purchase.external_order_id
    purchase.external_order_id = code
    _audit(
        entity="purchase", entity_id=purchase.id,
        field="external_order_id", old=old, new=code,
        admin_user_id=admin_user_id, reason=reason,
    )
    db.session.flush()
    return purchase


def correct_manual_shipment(
    *,
    shipment_id,
    carrier=None,
    tracking_code=None,
    reason,
    admin_user_id=None,
    expected_updated_at=None,
) -> Shipment:
    """Corrige transportadora e/ou rastreio (auditoria, sem efeitos)."""
    try:
        wanted = int(str(shipment_id))
    except (TypeError, ValueError):
        raise ValueError("shipment_invalid") from None
    shipment = db.session.query(Shipment).filter_by(
        id=wanted
    ).with_for_update(of=Shipment).populate_existing().first()
    if shipment is None:
        raise ValueError("shipment_not_found")
    _check_fresh(shipment, expected_updated_at)
    changed = False
    if carrier is not None:
        name = _require_text(carrier, field="carrier", maximum=60)
        if name != (shipment.carrier or ""):
            _audit(
                entity="shipment", entity_id=shipment.id, field="carrier",
                old=shipment.carrier, new=name,
                admin_user_id=admin_user_id, reason=reason,
            )
            shipment.carrier = name
            changed = True
    if tracking_code is not None:
        code = _norm_code(tracking_code, field="tracking_code")
        if code != shipment.tracking_code:
            clash = Shipment.query.filter_by(tracking_code=code).first()
            if clash is not None and clash.id != shipment.id:
                raise ValueError("tracking_code_in_use")
            _audit(
                entity="shipment", entity_id=shipment.id,
                field="tracking_code", old=shipment.tracking_code, new=code,
                admin_user_id=admin_user_id, reason=reason,
            )
            shipment.tracking_code = code
            changed = True
    if not changed:
        return shipment
    db.session.flush()
    return shipment


__all__ = [
    "BLOCKED_ORDER_STATUSES",
    "ELIGIBLE_ORDER_STATUSES",
    "ManualDispatchResult",
    "correct_manual_purchase",
    "correct_manual_shipment",
    "register_manual_purchase",
    "register_manual_shipment",
]
