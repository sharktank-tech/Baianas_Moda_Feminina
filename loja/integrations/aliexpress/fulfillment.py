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
    # Product deve ser AliExpress
    if product.source != SOURCE_ALIEXPRESS:
        return True, None, None, None  # Não é item AliExpress, ignora silenciosamente

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


def ensure_aliexpress_fulfillment(order: Order) -> List[FulfillmentResult]:
    """Cria/atualiza fulfillment AliExpress para um pedido pago.

    IDEMPOTENTE: roda múltiplas vezes sem duplicar.
    Deve ser chamado DENTRO da mesma transação do _sync_payment.
    """
    results: List[FulfillmentResult] = []

    # Fulfillment só é criado para pedidos pagos (chamado via _sync_payment).
    if order.status not in ("paid",):
        logger.info(
            "fulfillment: order %s status %r não gera fulfillment",
            order.id, order.status,
        )
        return results

    # Agrupar itens por supplier
    supplier_groups: dict[str, list[tuple[OrderItem, Product, ProductVariant, SupplierProduct]]] = {}

    for item in order.items:
        product = Product.query.filter_by(id=item.product_id).first()
        if not product:
            logger.warning(
                "fulfillment: item %s product_id %s não encontrado",
                item.id, item.product_id
            )
            continue

        variant = None
        if item.variant_id:
            variant = ProductVariant.query.filter_by(id=item.variant_id).first()

        is_valid, error_code, supplier, sp = _validate_aliexpress_item(item, product, variant)
        if not is_valid:
            # Item inválido será tratado no fulfillment por supplier
            supplier_groups.setdefault("__invalid__", []).append(
                (item, product, variant, error_code)
            )
            continue

        if supplier is None or sp is None:
            supplier_groups.setdefault("__invalid__", []).append(
                (item, product, variant, ERROR_CODES["ITEM_INTEGRITY_ERROR"])
            )
            continue

        supplier_groups.setdefault(supplier.id, []).append(
            (item, product, variant, sp)
        )

    # Processar cada grupo de supplier
    for supplier_id, items in supplier_groups.items():
        if supplier_id == "__invalid__":
            # Itens que não passaram na validação básica
            # Criar fulfillment bloqueado por supplier "desconhecido" não faz sentido
            # Apenas loga e continua
            for item, product, variant, error_code in items:
                logger.info(
                    "fulfillment: item %s product %s bloqueado: %s",
                    item.id, product.id, error_code
                )
            continue

        if not isinstance(supplier_id, uuid.UUID):
            logger.warning("fulfillment: supplier id inválido: %r", supplier_id)
            continue

        supplier = Supplier.query.filter_by(id=supplier_id).first()
        if not supplier:
            logger.warning("fulfillment: supplier %s não encontrado", supplier_id)
            continue

        # Verificar se já existe AliExpressOrder para este order+supplier
        idempotency_key = _generate_idempotency_key(str(order.id), supplier_id)
        ali_order = AliExpressOrder.query.filter_by(
            idempotency_key=idempotency_key
        ).first()

        now = utcnow()

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
        else:
            # Já existe: reutiliza (idempotência)
            # Resetar estado para reavaliação
            ali_order.state = "queued"
            ali_order.last_error = None
            ali_order.updated_at = now

        # Validar credencial
        credential_error: Optional[str] = None
        try:
            get_fulfillment_access_token(supplier.id)
        except CredentialNotConfigured:
            credential_error = ERROR_CODES["CREDENTIAL_MISSING"]
        except CredentialNotFound:
            credential_error = ERROR_CODES["CREDENTIAL_MISSING"]
        except CredentialExpired:
            credential_error = ERROR_CODES["CREDENTIAL_EXPIRED"]
        except CredentialInvalid:
            credential_error = ERROR_CODES["CREDENTIAL_INVALID"]

        # Criar/atualizar AliExpressOrderItem para cada item
        created_items: List[AliExpressOrderItem] = []

        for item, product, variant, sp in items:
            # Snapshot do SKU do fornecedor
            variant_label = None
            if variant:
                parts = []
                if variant.color:
                    parts.append(variant.color)
                if variant.size:
                    parts.append(variant.size)
                variant_label = " / ".join(parts) if parts else None

            # Verificar se já existe AliExpressOrderItem para este order_item
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
                    variant_label=variant_label,
                    state="queued",
                )
                db.session.add(ali_item)
            else:
                # Atualizar snapshot (idempotência - ordem já existe, mas snapshot pode mudar)
                ali_item.aliexpress_order_id = ali_order.id
                ali_item.product_id = product.id
                ali_item.variant_id = variant.id if variant else None
                ali_item.supplier_product_external_id = sp.supplier_product_id
                ali_item.supplier_sku_external_id = variant.supplier_sku_id if variant else ""
                ali_item.quantity = item.quantity
                ali_item.supplier_unit_cost = sp.cost
                ali_item.supplier_currency = sp.currency
                ali_item.variant_label = variant_label
                ali_item.state = "queued"
                ali_item.last_error = None
                ali_item.updated_at = now

            created_items.append(ali_item)

        db.session.flush()

        # Determinar estado final do AliExpressOrder
        if credential_error:
            ali_order.state = "blocked"
            ali_order.last_error = credential_error
        else:
            # Verificar integridade de todos os itens
            all_valid = True
            for ali_item in created_items:
                if not ali_item.supplier_product_external_id or not ali_item.supplier_sku_external_id:
                    all_valid = False
                    ali_item.state = "blocked"
                    ali_item.last_error = ERROR_CODES["ITEM_INTEGRITY_ERROR"]
                else:
                    ali_item.state = "ready"

            if all_valid:
                ali_order.state = "ready"
                ali_order.ready_at = now
                ali_order.last_error = None
            else:
                ali_order.state = "blocked"
                ali_order.last_error = "item_integrity_error"

        ali_order.updated_at = now
        results.append(FulfillmentResult(
            aliexpress_order=ali_order,
            items=created_items,
            state=ali_order.state
        ))

        logger.info(
            "fulfillment: order %s supplier %s -> state=%s items=%d",
            order.id, supplier_id, ali_order.state, len(created_items)
        )

    return results


def cancel_fulfillment_before_submission(order: Order, reason: str = "payment_refunded_before_submission") -> None:
    """Cancela fulfillments em estados pré-submissão quando pagamento é estornado.

    Estados afetados: queued, blocked, ready -> cancelled
    Estados NÃO afetados: submitting, submitted, confirmed, shipped -> cancel_requested/manual_review
    """
    ali_orders = AliExpressOrder.query.filter_by(order_id=order.id).all()
    now = utcnow()

    for ali_order in ali_orders:
        if ali_order.state in ("queued", "blocked", "ready"):
            ali_order.state = "cancelled"
            ali_order.cancelled_at = now
            ali_order.last_error = reason
            ali_order.updated_at = now
            # Itens também
            for item in ali_order.items:
                item.state = "cancelled"
                item.last_error = reason
                item.updated_at = now
        elif ali_order.state in ("submitting", "submitted", "confirmed", "shipped"):
            # Não podemos fingir cancelamento no fornecedor
            if ali_order.state in ("submitted", "confirmed", "shipped"):
                ali_order.state = "cancel_requested"
            else:
                ali_order.state = "manual_review"
            ali_order.last_error = f"payment_refunded_after_{ali_order.state}"
            ali_order.updated_at = now
            for item in ali_order.items:
                item.state = ali_order.state
                item.last_error = ali_order.last_error
                item.updated_at = now


__all__ = [
    "ensure_aliexpress_fulfillment",
    "cancel_fulfillment_before_submission",
    "FulfillmentResult",
    "ERROR_CODES",
]