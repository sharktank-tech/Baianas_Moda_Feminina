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
    "ITEM_INTEGRITY_ERROR": "item_integrity_error",
}


@dataclass
class FulfillmentResult:
    aliexpress_order: AliExpressOrder
    items: List[AliExpressOrderItem]
    state: str  # ready | blocked


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


def _process_valid_group(
    order: Order,
    supplier_id: uuid.UUID,
    items: list[tuple[OrderItem, Product, ProductVariant, SupplierProduct]],
    now,
) -> Optional[FulfillmentResult]:
    """Cria/atualiza o AliExpressOrder de um supplier com itens válidos."""
    if not isinstance(supplier_id, uuid.UUID):
        logger.warning("fulfillment: supplier id inválido: %r", supplier_id)
        return None

    supplier = Supplier.query.filter_by(id=supplier_id).first()
    if not supplier:
        logger.warning("fulfillment: supplier %s não encontrado", supplier_id)
        return None

    idempotency_key = _generate_idempotency_key(str(order.id), supplier_id)
    ali_order = AliExpressOrder.query.filter_by(idempotency_key=idempotency_key).first()

    if ali_order is None:
        ali_order = AliExpressOrder(
            id=uuid.uuid4(),
            order_id=order.id,
            supplier_id=supplier.id,
            state="queued",
            idempotency_key=idempotency_key,
            queued_at=now,
        )
        db.session.add(ali_order)
        db.session.flush()
    elif ali_order.state not in REVALIDATABLE_STATES:
        # Fase 8.0.1: estado avançado (submitting/submitted/…) não regride —
        # nem por webhook repetido, nem por revalidação.
        logger.info(
            "fulfillment: order %s supplier %s em estado %r não revalidado",
            order.id, supplier_id, ali_order.state,
        )
        return None
    else:
        ali_order.state = "queued"
        ali_order.last_error = None
        ali_order.updated_at = now

    credential_error = _check_credential(supplier.id)

    created_items: List[AliExpressOrderItem] = []

    for item, product, variant, sp in items:
        ali_item = AliExpressOrderItem.query.filter_by(
            order_item_id=item.id
        ).first()

        if ali_item is None:
            ali_item = AliExpressOrderItem(
                id=uuid.uuid4(),
                aliexpress_order_id=ali_order.id,
                order_item_id=item.id,
                product_id=product.id,
                variant_id=variant.id if variant else None,
                supplier_product_external_id=sp.supplier_product_id,
                supplier_sku_external_id=variant.supplier_sku_id if variant else "",
                quantity=item.quantity,
                supplier_unit_cost=sp.cost,
                supplier_currency=sp.currency,
                variant_label=_variant_label(variant),
                state="queued",
            )
            db.session.add(ali_item)
        elif ali_item.aliexpress_order_id != ali_order.id:
            # Fase 8.0.1: linha pertence a outro AliExpressOrder — não relinka
            # silenciosamente; marca integridade para revisão manual.
            ali_item.state = "manual_review"
            ali_item.last_error = ERROR_CODES["ITEM_INTEGRITY_ERROR"]
            ali_item.updated_at = now
        else:
            # Snapshot imutável (Fase 8.0.1): supplier_product_external_id,
            # supplier_sku_external_id, quantity, supplier_unit_cost,
            # supplier_currency, variant_label, product_id e variant_id NUNCA
            # são reescritos em re-ensure. Só estado/erro/timestamp mudam.
            ali_item.state = "queued"
            ali_item.last_error = None
            ali_item.updated_at = now

        created_items.append(ali_item)

    db.session.flush()

    if credential_error:
        ali_order.state = "blocked"
        ali_order.last_error = credential_error
    else:
        all_valid = True
        for ali_item in created_items:
            if ali_item.state == "manual_review":
                all_valid = False
                continue
            if (
                not ali_item.supplier_product_external_id
                or not ali_item.supplier_sku_external_id
            ):
                all_valid = False
                ali_item.state = "blocked"
                ali_item.last_error = ERROR_CODES["ITEM_INTEGRITY_ERROR"]
            else:
                ali_item.state = "ready"

        if all_valid and created_items:
            ali_order.state = "ready"
            ali_order.ready_at = now
            ali_order.last_error = None
        else:
            ali_order.state = "blocked"
            ali_order.last_error = ERROR_CODES["ITEM_INTEGRITY_ERROR"]

    ali_order.updated_at = now

    result = FulfillmentResult(
        aliexpress_order=ali_order,
        items=created_items,
        state=ali_order.state,
    )
    logger.info(
        "fulfillment: order %s supplier %s -> state=%s items=%d",
        order.id, supplier_id, ali_order.state, len(created_items),
    )
    return result


def _process_invalid_items(
    order: Order,
    invalid_items: list[tuple[OrderItem, Product, Optional[ProductVariant], str]],
    now,
) -> List[FulfillmentResult]:
    """Itens AliExpress inválidos viram AliExpressOrder 'blocked' visível na fila.

    Estratégia A: atribui a um único supplier AliExpress válido (SupplierProduct
    ativo). Estratégia B: supplier_id=NULL com idempotency_key determinística
    'unattributed'. Nunca inventa supplier/SKU e nunca cria item snapshot
    (não há como congelar SKU inexistente)."""
    results: List[FulfillmentResult] = []
    if not invalid_items:
        return results

    blocks: dict[object, dict] = {}
    for item, product, variant, error_code in invalid_items:
        supplier_id = _infer_invalid_supplier(item, product, variant)
        key: object = supplier_id if supplier_id is not None else "__unattributed__"
        block = blocks.setdefault(key, {
            "supplier_id": supplier_id,
            "errors": set(),
            "items": [],
        })
        block["errors"].add(error_code)
        block["items"].append(item)

    for key, block in blocks.items():
        supplier_id = block["supplier_id"]
        if supplier_id is not None:
            idempotency_key = _generate_idempotency_key(str(order.id), supplier_id)
        else:
            idempotency_key = _generate_unattributed_idempotency_key(str(order.id))

        ali_order = AliExpressOrder.query.filter_by(
            idempotency_key=idempotency_key
        ).first()

        if ali_order is None:
            ali_order = AliExpressOrder(
                id=uuid.uuid4(),
                order_id=order.id,
                supplier_id=supplier_id,
                state="blocked",
                idempotency_key=idempotency_key,
                queued_at=now,
            )
            db.session.add(ali_order)
            db.session.flush()
        elif ali_order.state not in REVALIDATABLE_STATES:
            logger.info(
                "fulfillment: order %s em estado %r não regride para blocked",
                order.id, ali_order.state,
            )
            continue

        ali_order.state = "blocked"
        ali_order.last_error = ",".join(sorted(block["errors"]))
        ali_order.updated_at = now

        results.append(FulfillmentResult(
            aliexpress_order=ali_order,
            items=[],
            state="blocked",
        ))
        logger.info(
            "fulfillment: order %s itens inválidos -> state=blocked errors=%s",
            order.id, ali_order.last_error,
        )

    return results


def ensure_aliexpress_fulfillment(order: Order) -> List[FulfillmentResult]:
    """Cria/atualiza fulfillment AliExpress para um pedido pago.

    IDEMPOTENTE: roda múltiplas vezes sem duplicar.
    Deve ser chamado DENTRO da mesma transação do _sync_payment.

    Fase 8.0.1:
      * Itens AliExpress inválidos geram AliExpressOrder 'blocked' visível
        na fila (estratégia A: supplier único; estratégia B: unattributed).
      * Produtos internos (source != aliexpress) são ignorados ANTES da
        validação — nunca geram blocked.
      * Snapshot do item é imutável em re-ensure.
      * Estados avançados (submitting, submitted, …) nunca regridem.
    """
    results: List[FulfillmentResult] = []

    # Fulfillment só é criado para pedidos pagos (chamado via _sync_payment).
    if order.status not in ("paid",):
        logger.info(
            "fulfillment: order %s status %r não gera fulfillment",
            order.id, order.status,
        )
        return results

    valid_groups: dict[uuid.UUID, list] = {}
    invalid_items: list[tuple[OrderItem, Product, Optional[ProductVariant], str]] = []

    for item in order.items:
        product = Product.query.filter_by(id=item.product_id).first()
        if not product:
            logger.warning(
                "fulfillment: item %s product_id %s não encontrado",
                item.id, item.product_id
            )
            continue

        # Produtos internos fora do escopo do fulfillment AliExpress. Ignorados
        # ANTES de qualquer validação (Fase 8.0.1): sem __invalid__, sem blocked.
        if product.source != SOURCE_ALIEXPRESS:
            continue

        variant = None
        if item.variant_id:
            variant = ProductVariant.query.filter_by(id=item.variant_id).first()

        is_valid, error_code, supplier, sp = _validate_aliexpress_item(
            item, product, variant
        )
        if not is_valid:
            invalid_items.append((item, product, variant, error_code))
            continue

        if supplier is None or sp is None:
            invalid_items.append(
                (item, product, variant, ERROR_CODES["ITEM_INTEGRITY_ERROR"])
            )
            continue

        valid_groups.setdefault(supplier.id, []).append(
            (item, product, variant, sp)
        )

    now = utcnow()

    # 1) Grupos válidos (um AliExpressOrder por supplier, idempotente).
    for supplier_id, items in valid_groups.items():
        result = _process_valid_group(order, supplier_id, items, now)
        if result is not None:
            results.append(result)

    # 2) Itens inválidos -> blocked visível na fila.
    results.extend(_process_invalid_items(order, invalid_items, now))

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