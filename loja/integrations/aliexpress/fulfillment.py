"""Serviço de fulfillment AliExpress (Fase 8.0).

ensure_aliexpress_fulfillment(order) -> IDEMPOTENTE.

Pipeline:
  1. Para cada OrderItem do pedido:
     A. Carrega Product
     B. Se Product.source != "aliexpress": ignora
     C. Para AliExpress:
        - variant_id obrigatório
        - ProductVariant deve existir e pertencer ao Product
        - supplier_product_id obrigatório
        - supplier_sku_id obrigatório
        - SupplierProduct ativo
        - Supplier channel_type == "aliexpress"
        - seller_id presente
  2. Agrupa itens por Supplier
  3. Para cada grupo (um Supplier):
     - Cria/atualiza AliExpressOrder (idempotency_key determinística)
     - Cria AliExpressOrderItem (snapshot) para cada OrderItem
     - Valida credencial (existe, descriptografável, não expirada)
     - Define state: ready | blocked (com last_error sanitizado)
  4. Retorna lista de AliExpressOrder criados/atualizados

NÃO faz chamada HTTP para AliExpress (hard off nesta fase).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import func

from loja import db
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)
from loja.utils import utcnow

from .credential_store import (
    get_fulfillment_access_token,
    CredentialNotConfigured,
    CredentialNotFound,
    CredentialExpired,
    CredentialInvalid,
)
from .oauth_refresh import evaluate_credential_with_refresh
from .sku_attr import build_sku_attr as _build_sku_attr

logger = logging.getLogger(__name__)

SOURCE_ALIEXPRESS = "aliexpress"


# Códigos de erro sanitizados para last_error
ERROR_CODES = {
    "CREDENTIAL_MISSING": "credential_missing",
    "CREDENTIAL_EXPIRED": "credential_expired",
    "CREDENTIAL_INVALID": "credential_invalid",
    "VARIANT_MISSING": "variant_missing",
    "VARIANT_SUPPLIER_MISSING": "variant_supplier_missing",
    "SUPPLIER_PRODUCT_MISSING": "supplier_product_missing",
    "SUPPLIER_SKU_MISSING": "supplier_sku_missing",
    "SUPPLIER_MISMATCH": "supplier_mismatch",
    "SUPPLIER_SKU_ATTR_MISSING": "supplier_sku_attr_missing",
    "ITEM_INTEGRITY_ERROR": "item_integrity_error",
    "SUBMISSION_HARD_OFF": "submission_hard_off",
    "SUBMISSION_FAILED": "submission_failed",
    "SUBMISSION_ERROR": "submission_error",
    "SUBMISSION_REQUIRES_SUPPLIER": "submission_requires_supplier",
    "SUBMISSION_UNKNOWN": "submission_unknown",
    "RECONCILIATION_UNKNOWN": "reconciliation_unknown",
    "RECONCILIATION_ERROR": "reconciliation_error",
    "STATUS_SYNC_HARD_OFF": "status_sync_hard_off",
    "STATUS_SYNC_FAILED": "status_sync_failed",
    "STATUS_SYNC_ERROR": "status_sync_error",
    "STATUS_SYNC_UNKNOWN": "status_sync_unknown",
    "STATUS_SYNC_INVALID_TRANSITION": "status_sync_invalid_transition",
    "SUBMISSION_SNAPSHOT_MISSING": "submission_snapshot_missing",
    "SUBMISSION_SNAPSHOT_INVALID": "submission_snapshot_invalid",
    "PAYMENT_GUARD_NOT_PAID": "payment_guard_not_paid",
    "PAYMENT_GUARD_NO_APPROVED_PAYMENT": "payment_guard_no_approved_payment",
    "PAYMENT_GUARD_MANUAL_REVIEW": "payment_guard_manual_review",
    "PAYMENT_GUARD_VALUE_MISMATCH": "payment_guard_value_mismatch",
    "PAYMENT_GUARD_CURRENCY_MISMATCH": "payment_guard_currency_mismatch",
}


@dataclass
class FulfillmentResult:
    aliexpress_order: AliExpressOrder
    items: List[AliExpressOrderItem]
    state: str  # ready | submitting | submitted | blocked


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _generate_idempotency_key(order_id: str, supplier_id: str) -> str:
    """Chave determinística: sha256('fulfillment:v1:<order_id>:<supplier_id>')."""
    base = f"fulfillment:v1:{order_id}:{supplier_id}"
    return _sha256_hex(base)


def _validate_aliexpress_item(
    item: OrderItem,
    product: Product,
    variant: Optional[ProductVariant],
) -> tuple[bool, Optional[str], Optional[Supplier], Optional[SupplierProduct]]:
    """Valida um OrderItem AliExpress.

    Retorna: (is_valid, error_code, supplier, supplier_product)
    """
    # Product deve ser AliExpress. Produtos internos são ignorados ANTES de
    # chegar aqui (ver ensure_aliexpress_fulfillment, pré-filtro por source).
    # Este retorno é apenas uma rede de segurança — não cria grupo nem blocked.
    if product.source != SOURCE_ALIEXPRESS:
        return True, None, None, None

    # Variant obrigatório para AliExpress
    if variant is None:
        return False, ERROR_CODES["VARIANT_MISSING"], None, None

    # Variant deve pertencer ao Product
    if variant.product_id != product.id:
        return False, ERROR_CODES["ITEM_INTEGRITY_ERROR"], None, None

    # SupplierProduct obrigatório
    sp = SupplierProduct.query.filter_by(
        id=variant.supplier_product_id
    ).first() if variant.supplier_product_id else None

    if sp is None:
        return False, ERROR_CODES["SUPPLIER_PRODUCT_MISSING"], None, None

    if not sp.is_active:
        return False, ERROR_CODES["SUPPLIER_PRODUCT_MISSING"], None, None

    # Supplier SKU obrigatório
    if not variant.supplier_sku_id:
        return False, ERROR_CODES["SUPPLIER_SKU_MISSING"], None, None

    # Supplier deve ser AliExpress
    supplier = sp.supplier
    if supplier is None or supplier.channel_type != SOURCE_ALIEXPRESS:
        return False, ERROR_CODES["SUPPLIER_MISMATCH"], None, None

    if not supplier.seller_id:
        return False, ERROR_CODES["SUPPLIER_MISMATCH"], None, None

    return True, None, supplier, sp


REVALIDATABLE_STATES = frozenset({"queued", "blocked", "ready"})


def _variant_label(variant: Optional[ProductVariant]) -> Optional[str]:
    """Label sanitizado (cor/tamanho) para o snapshot."""
    if variant is None:
        return None
    parts = []
    if variant.color:
        parts.append(variant.color)
    if variant.size:
        parts.append(variant.size)
    return " / ".join(parts) if parts else None


def _generate_unattributed_idempotency_key(order_id: str) -> str:
    """Chave determinística p/ itens inválidos sem supplier atribuível:
    sha256('fulfillment:v1:<order_id>:unattributed')."""
    base = f"fulfillment:v1:{order_id}:unattributed"
    return _sha256_hex(base)


def _resolve_single_aliexpress_supplier(product: Product) -> Optional[uuid.UUID]:
    """Estratégia A (Fase 8.0.1): infere o único supplier AliExpress válido do
    produto via SupplierProducts ativos. Retorna None quando 0 ou >1 (nesses
    casos o item inválido vai para estratégia B — unattributed)."""
    supplier_ids = {
        sp.supplier_id
        for sp in SupplierProduct.query.filter_by(
            product_id=product.id, is_active=True
        ).all()
        if sp.supplier_id is not None
    }
    valid_ids = set()
    for sid in supplier_ids:
        supplier = Supplier.query.filter_by(id=sid).first()
        if (
            supplier is not None
            and supplier.channel_type == SOURCE_ALIEXPRESS
            and supplier.seller_id
        ):
            valid_ids.add(sid)
    if len(valid_ids) == 1:
        return next(iter(valid_ids))
    return None


def _infer_invalid_supplier(
    item: OrderItem,
    product: Product,
    variant: Optional[ProductVariant],
) -> Optional[uuid.UUID]:
    """Tenta atribuir item AliExpress inválido a um único supplier válido.

    Prioridade: supplier do SupplierProduct da variante (quando conhecido E
    AliExpress válido); senão estratégia A (SupplierProducts ativos do produto);
    senão None -> estratégia B (unattributed). Nunca inventa supplier/SKU."""
    if variant is not None and variant.supplier_product_id:
        sp = SupplierProduct.query.filter_by(id=variant.supplier_product_id).first()
        if sp is not None and sp.is_active and sp.supplier_id is not None:
            supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
            if (
                supplier is not None
                and supplier.channel_type == SOURCE_ALIEXPRESS
                and supplier.seller_id
            ):
                return supplier.id
    return _resolve_single_aliexpress_supplier(product)


def _check_credential(supplier_id) -> Optional[str]:
    """Valida credencial e devolve erro sanitizado (None se ok)."""
    try:
        get_fulfillment_access_token(supplier_id)
    except CredentialNotConfigured:
        return ERROR_CODES["CREDENTIAL_MISSING"]
    except CredentialNotFound:
        return ERROR_CODES["CREDENTIAL_MISSING"]
    except CredentialExpired:
        return ERROR_CODES["CREDENTIAL_EXPIRED"]
    except CredentialInvalid:
        return ERROR_CODES["CREDENTIAL_INVALID"]
    return None


def _resolve_existing_ali_orders(order: Order) -> list[AliExpressOrder]:
    """Lock dos AliExpressOrders existentes do pedido (controle de unicidade).

    PostgreSQL: FOR UPDATE evita corrida em promoção unattributed->supplier.
    SQLite: sem efeito (mas garante a consulta)."""
    return (
        AliExpressOrder.query.filter_by(order_id=order.id).with_for_update().all()
    )


def _validate_payment_guard(order: Order) -> Optional[str]:
    """Validação canônica de pagamento antes do fulfillment.

    Retorna código de erro sanitizado se o guard falhar, None se válido.

    Condições obrigatórias (fail-closed):
    - Order.status == "paid"
    - Evento financeiro vigente do pedido aprovado, com valor/moeda corretos
    - Pagamento pertencente ao mesmo pedido (order_id match)
    - Ausência de divergência/reconciliação pendente (Order.status != "manual_review")

    Política de múltiplos pagamentos (conforme _sync_payment):
    _sync_payment atualiza o MESMO registro por provider_payment_id único;
    múltiplos registros = múltiplos provider payments. O estado vigente é o
    registro mais recente (created_at desc, id desc como desempate
    determinístico); só depois valida-se se está aprovado. Nunca se filtra
    approved antes de escolher o evento vigente.
    """
    # 1. Estados terminais que bloqueiam fulfillment
    if order.status == "manual_review":
        return ERROR_CODES["PAYMENT_GUARD_MANUAL_REVIEW"]
    if order.status in ("refunded", "charged_back", "cancelled", "payment_failed"):
        return ERROR_CODES["PAYMENT_GUARD_NOT_PAID"]

    # 2. Status deve ser exatamente "paid"
    if order.status != "paid":
        return ERROR_CODES["PAYMENT_GUARD_NOT_PAID"]

    # 3. Evento financeiro vigente: registro mais recente do pedido,
    #    independente de status; desempate determinístico por id.
    payment = (
        Payment.query.filter_by(order_id=order.id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .first()
    )
    if payment is None or payment.status != "approved":
        return ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]

    # 4. Pagamento deve pertencer a este pedido (order_id match já garantido pela query,
    #    mas validação defensiva de provider_payment_id)
    if str(payment.provider_payment_id or "").strip() == "":
        return ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]

    # 5. Valor/moeda devem conferir com o pedido (redundância defensiva no boundary)
    if payment.currency != "BRL":
        return ERROR_CODES["PAYMENT_GUARD_CURRENCY_MISMATCH"]
    if payment.amount != order.total:
        return ERROR_CODES["PAYMENT_GUARD_VALUE_MISMATCH"]

    return None


def ensure_aliexpress_fulfillment(
    order: Order,
    refresher: Optional[AliExpressOAuthRefresher] = None,
) -> List[FulfillmentResult]:
    """Consolida o fulfillment AliExpress de um pedido pago (Fase 8.0.2).

    SNAPSHOT CANÔNICO:
      * Após existir AliExpressOrderItem válido para um OrderItem, o SNAPSHOT é
        a fonte da verdade — o catálogo (variante/supplier product/custo) NÃO é
        consultado de novo para revalidar. supplier vem de AliExpressOrder.supplier_id;
        supplier product/SKU/custo/moeda/quantidade/label vêm do snapshot.
      * O estado final é decidido por: integridade interna do snapshot + estado +
        credencial. Catálogo só é usado na PRIMEIRA criação do snapshot.
      * Credencial ainda pode mudar o estado (ready <-> blocked) sem depender
        do catálogo atual.

    REFRESH OAuth (Fase 8.0.4):
      * ``refresher`` opcional. Quando informado, credencial expirada com
        refresh token disponível pode tentar renovação síncrona; com sucesso, a
        mesma AliExpressOrder volta para ``ready`` sem revalidar o catálogo.
      * Sem refresher, comportamento idêntico à Fase 8.0.2/8.0.3 (sem
        renovação automática; provider padrão de refresh é HARD-OFF).
      * ``item_integrity_error``/bloqueio estrutural SEMPRE prevalece sobre
        credencial: integridade -> credencial -> submissão.

    CONSOLIDAÇÃO (item 5):
      * Um único resultado final por AliExpressOrder: itens válidos + inválidos
        do MESMO supplier são consolidados; se qualquer item (ou credencial)
        falhar -> final BLOCKED com código(s) sanitizado(s).

    PROMOÇÃO (item 4):
      * AliExpressOrder unattributed (sem snapshot, supplier NULL) que passa a
        ter um único supplier resolvível é PROMOVIDO (mesmo id): preenche
        supplier_id e troca idempotency_key p/ supplier-esp., criando os
        snapshots. Sob lock/unicidade; nunca duplica fulfillment para o mesmo
        problema.

    Estados avançados (submitting/submitted/…) nunca regridem.

    NÃO faz chamada HTTP para AliExpress (hard off nesta fase).
    """
    if order.status not in ("paid",):
        logger.info(
            "fulfillment: order %s status %r não gera fulfillment",
            order.id, order.status,
        )
        return []

    # Validação canônica de pagamento (fail-closed no boundary de serviço)
    payment_guard_error = _validate_payment_guard(order)
    if payment_guard_error is not None:
        logger.info(
            "fulfillment: bloqueado pelo payment guard: %s",
            payment_guard_error,
        )
        return []

    order_items = list(order.items)
    if not order_items:
        return []

    now = utcnow()

    # Lock dos AliExpressOrders existentes (unicidade).
    existing_orders = _resolve_existing_ali_orders(order)
    by_order_id = {o.id: o for o in existing_orders}

    # Mapas de resolução.
    sid_order_id: dict[uuid.UUID, uuid.UUID] = {}
    unattributed_id: Optional[uuid.UUID] = None
    for o in existing_orders:
        if (
            o.supplier_id is not None
            and o.idempotency_key == _generate_idempotency_key(str(order.id), o.supplier_id)
        ):
            sid_order_id[o.supplier_id] = o.id
        if o.idempotency_key == _generate_unattributed_idempotency_key(str(order.id)):
            unattributed_id = o.id

    # Snapshots já existentes (canônicos) dos OrderItems deste pedido.
    snapshots: dict[uuid.UUID, AliExpressOrderItem] = {
        a.order_item_id: a
        for a in AliExpressOrderItem.query.filter(
            AliExpressOrderItem.order_item_id.in_([it.id for it in order_items])
        ).all()
    }

    # Cada entry: (group_key, outcome)
    #   group_key: ("sid", ali_order_id) | ("sp", supplier_id) | ("unattributed",)
    entries: list = []

    for item in order_items:
        snap = snapshots.get(item.id)
        product = (
            Product.query.filter_by(id=item.product_id).first()
            if item.product_id else None
        )

        # --- Modo snapshot canônico (Fase 8.0.10.1: nunca relê catálogo) ---
        if snap is not None:
            ao = snap.aliexpress_order
            if ao.state not in REVALIDATABLE_STATES:
                continue  # estado avançado: não regride nem revalida
            if (
                snap.supplier_product_external_id
                and snap.supplier_sku_external_id
                and snap.supplier_sku_attr
                and str(snap.supplier_sku_attr).strip()
            ):
                entries.append((("sid", ao.id), ("snapshot_ready", snap)))
            else:
                # Snapshot legado/incompleto: fail closed, sem lazy backfill
                code = ERROR_CODES["ITEM_INTEGRITY_ERROR"]
                if not snap.supplier_sku_attr or not str(snap.supplier_sku_attr).strip():
                    # Distingue quando possível, mas preserva precedência de integridade
                    if snap.supplier_product_external_id and snap.supplier_sku_external_id:
                        code = ERROR_CODES["SUPPLIER_SKU_ATTR_MISSING"]
                entries.append((
                    ("sid", ao.id),
                    ("snapshot_invalid", snap, code),
                ))
            continue

        # Sem snapshot: produto interno é ignorado (não gera fulfillment).
        if product is None or product.source != SOURCE_ALIEXPRESS:
            continue

        # --- Primeira criação do snapshot (catálogo é consultado aqui) ---
        variant = (
            ProductVariant.query.filter_by(id=item.variant_id).first()
            if item.variant_id else None
        )
        is_valid, error_code, supplier, sp = _validate_aliexpress_item(
            item, product, variant
        )

        if is_valid and supplier is not None and sp is not None:
            # Congela identidade de compra somente na PRIMEIRA materialização
            sku_attr = _build_sku_attr(getattr(variant, "attributes", None))
            if not sku_attr:
                # fail closed: não inventa IDs por labels, não cria snapshot válido
                sku_error = ERROR_CODES["SUPPLIER_SKU_ATTR_MISSING"]
                supplier_id = _infer_invalid_supplier(item, product, variant)
                if supplier_id is not None:
                    entries.append((("sp", supplier_id), ("new_invalid", item, sku_error)))
                else:
                    entries.append((("unattributed",), ("new_invalid", item, sku_error)))
                continue
            snapshot_data = {
                "product_id": product.id,
                "variant_id": variant.id,
                "supplier_product_external_id": sp.supplier_product_id,
                "supplier_sku_external_id": variant.supplier_sku_id,
                "supplier_sku_attr": sku_attr,
                "quantity": item.quantity,
                "supplier_unit_cost": sp.cost,
                "supplier_currency": sp.currency,
                "variant_label": _variant_label(variant),
            }
            entries.append((("sp", supplier.id), ("new_valid", item, snapshot_data)))
        else:
            supplier_id = (
                _infer_invalid_supplier(item, product, variant)
                if product is not None else None
            )
            if supplier_id is not None:
                entries.append((("sp", supplier_id), ("new_invalid", item, error_code)))
            else:
                entries.append((("unattributed",), ("new_invalid", item, error_code)))

    # --- Resolver cada group_key para um AliExpressOrder concreto ---
    resolution: dict = {}

    # Se nesta rodada há item inválido SEM supplier atribuível, a ordem
    # unattributed não é promovida: não fundir itens unattributed numa ordem de
    # supplier (mesmo guard de "não fundir" quando já existe ordem supplier-esp.).
    has_unattributed_entries = any(k[0] == "unattributed" for k, _ in entries)

    def _resolve_group(key) -> Optional[AliExpressOrder]:
        if key in resolution:
            return resolution[key]

        if key[0] == "sid":
            ao = by_order_id.get(key[1])
            resolution[key] = ao
            return ao

        if key[0] == "sp":
            sid: uuid.UUID = key[1]
            existing_id = sid_order_id.get(sid)
            if existing_id is not None:
                ao = by_order_id.get(existing_id)
                if ao is not None and ao.state not in REVALIDATABLE_STATES:
                    # Estado avançado: não regride nem cria item aqui.
                    resolution[key] = None
                    return None
                resolution[key] = ao
                return ao
            if unattributed_id is not None and not has_unattributed_entries:
                # Promoção (item 4): mesmo AliExpressOrder unattributed vira do
                # supplier — preenche supplier_id + idempotency_key supplier-esp.
                ao = by_order_id.get(unattributed_id)
                if ao is not None:
                    if ao.state not in REVALIDATABLE_STATES:
                        resolution[key] = None
                        return None
                    ao.supplier_id = sid
                    ao.idempotency_key = _generate_idempotency_key(str(order.id), sid)
                    sid_order_id[sid] = ao.id
                    resolution[key] = ao
                    return ao
            ao = AliExpressOrder(
                id=uuid.uuid4(),
                order_id=order.id,
                supplier_id=sid,
                state="queued",
                idempotency_key=_generate_idempotency_key(str(order.id), sid),
                queued_at=now,
            )
            db.session.add(ao)
            db.session.flush()
            sid_order_id[sid] = ao.id
            resolution[key] = ao
            return ao

        # unattributed
        if unattributed_id is not None:
            ao = by_order_id.get(unattributed_id)
            resolution[key] = ao
            return ao
        ao = AliExpressOrder(
            id=uuid.uuid4(),
            order_id=order.id,
            supplier_id=None,
            state="queued",
            idempotency_key=_generate_unattributed_idempotency_key(str(order.id)),
            queued_at=now,
        )
        db.session.add(ao)
        db.session.flush()
        resolution[key] = ao
        return ao

    # --- Agrupar outcomes por AliExpressOrder (1 resultado final por order) ---
    groups: dict[uuid.UUID, dict] = {}
    for key, outcome in entries:
        ao = _resolve_group(key)
        if ao is None:
            continue
        g = groups.get(ao.id)
        if g is None:
            g = {"ao": ao, "supplier_id": ao.supplier_id, "outcomes": []}
            groups[ao.id] = g
        if ao.supplier_id is not None:
            g["supplier_id"] = ao.supplier_id
        g["outcomes"].append(outcome)

    results: List[FulfillmentResult] = []

    for gkey, g in groups.items():
        ao = g["ao"]
        if ao.state not in REVALIDATABLE_STATES:
            continue  # estado avançado não regride
        sid = g["supplier_id"]

        ao.state = "queued"
        ao.last_error = None
        ao.updated_at = now

        error_codes: set = set()
        created_items: List[AliExpressOrderItem] = []
        has_valid_item = False

        for outcome in g["outcomes"]:
            kind = outcome[0]
            if kind == "snapshot_ready":
                snap = outcome[1]
                snap.state = "ready"
                snap.last_error = None
                snap.updated_at = now
                created_items.append(snap)
                has_valid_item = True
            elif kind == "snapshot_invalid":
                snap, err = outcome[1], outcome[2]
                snap.state = "blocked"
                snap.last_error = err
                snap.updated_at = now
                error_codes.add(err)
                created_items.append(snap)
            elif kind == "new_valid":
                item, data = outcome[1], outcome[2]
                snap = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
                if snap is None:
                    snap = AliExpressOrderItem(
                        id=uuid.uuid4(),
                        aliexpress_order_id=ao.id,
                        order_item_id=item.id,
                        product_id=data["product_id"],
                        variant_id=data["variant_id"],
                        supplier_product_external_id=data["supplier_product_external_id"],
                        supplier_sku_external_id=data["supplier_sku_external_id"],
                        supplier_sku_attr=data["supplier_sku_attr"],
                        quantity=data["quantity"],
                        supplier_unit_cost=data["supplier_unit_cost"],
                        supplier_currency=data["supplier_currency"],
                        variant_label=data["variant_label"],
                        state="queued",
                    )
                    db.session.add(snap)
                snap.state = "ready"
                snap.last_error = None
                snap.updated_at = now
                created_items.append(snap)
                has_valid_item = True
            elif kind == "new_invalid":
                _item, err = outcome[1], outcome[2]
                error_codes.add(err)

        # Credencial só decide o estado quando há ao menos um snapshot válido
        # (item 3). Grupo só com itens inválidos permanece blocked pelo erro do
        # próprio item, sem misturar código de credencial.
        if has_valid_item and sid is not None:
            if refresher is None:
                credential_error = _check_credential(sid)
            else:
                credential_error = evaluate_credential_with_refresh(sid, refresher)
            if credential_error:
                error_codes.add(credential_error)

        db.session.flush()

        if error_codes:
            ao.state = "blocked"
            ao.last_error = ",".join(sorted(error_codes))
        else:
            ao.state = "ready"
            ao.ready_at = now
            ao.last_error = None

        ao.updated_at = now
        results.append(FulfillmentResult(
            aliexpress_order=ao,
            items=created_items,
            state=ao.state,
        ))
        logger.info(
            "fulfillment: order %s -> state=%s items=%d error=%s",
            order.id, ao.state, len(created_items), ao.last_error,
        )

    return results


def cancel_fulfillment_before_submission(order: Order, reason: str = "payment_refunded_before_submission") -> None:
    """Cancela fulfillments quando o pagamento é estornado antes da submissão.

    Estados pré-submissão (queued, blocked, ready) -> cancelled.
    Estados avançados:
      * submitting -> manual_review (código payment_refunded_after_submitting)
      * submitted/confirmed/shipped/delivered -> cancel_requested
        (código payment_refunded_after_<estado_anterior>)
    Estados já em cancelamento/review não são alterados.

    Fase 8.0.1: o código de erro usa o estado ANTERIOR à mudança (nunca
    'payment_refunded_after_cancel_requested').
    """
    ali_orders = AliExpressOrder.query.filter_by(order_id=order.id).all()
    now = utcnow()

    for ali_order in ali_orders:
        previous_state = ali_order.state

        if previous_state in ("queued", "blocked", "ready"):
            ali_order.state = "cancelled"
            ali_order.cancelled_at = now
            ali_order.last_error = reason
            ali_order.updated_at = now
            for item in ali_order.items:
                item.state = "cancelled"
                item.last_error = reason
                item.updated_at = now
        elif previous_state == "submitting":
            ali_order.state = "manual_review"
            ali_order.last_error = "payment_refunded_after_submitting"
            ali_order.updated_at = now
            for item in ali_order.items:
                item.state = "manual_review"
                item.last_error = ali_order.last_error
                item.updated_at = now
        elif previous_state in ("submitted", "confirmed", "shipped", "delivered"):
            ali_order.state = "cancel_requested"
            ali_order.last_error = f"payment_refunded_after_{previous_state}"
            ali_order.updated_at = now
            for item in ali_order.items:
                item.state = "cancel_requested"
                item.last_error = ali_order.last_error
                item.updated_at = now
        # cancel_requested / cancelled / manual_review / …: mantém como está.


__all__ = [
    "ensure_aliexpress_fulfillment",
    "cancel_fulfillment_before_submission",
    "FulfillmentResult",
    "ERROR_CODES",
]