"""Sincronização técnica de produto PUBLICADO (Etapa 3, integração AliExpress).

Atualiza SOMENTE dado técnico/estoque de um produto que já está à venda,
a partir de um `AliExpressProductDetail` JÁ obtido pela rota (este módulo não
tem credenciais, não constrói cliente e não faz nenhuma chamada de fornecedor,
frete, compra ou pedido).

ATUALIZADO (técnico):
  * `ProductVariant.stock` (SKU do payload) e desativação de variante com
    estoque zero ou SKU sumido do catálogo; novos SKUs entram INATIVOS;
  * `Product.stock` (soma dos estoques das variantes ativas);
  * `ProductSync.last_synced_at` / `error` (nunca volta a "draft");
  * `sync_status`/`last_sync_at`/`sync_error` do produto e do vínculo.

PRESERVADO (decisão comercial — nunca escrito aqui):
  * nome, descrição, imagens, categoria;
  * `price`, `target_margin_percent`, `currency`, `cost`,
    `pricing_status`/`pricing_error` (nunca vira `stale` sozinho);
  * `is_draft`/`is_active` (NUNCA despublica), frete/landed/evidência;
  * origem de envio e seleção de operação nacional;
  * atributos/cor/tamanho/preço de variante já existentes, e reativação de
    variante desativada (o produto nunca ganha venda nova por sincronização).

DIVERGÊNCIA DE CUSTO/MOEDA: comparada e SINALIZADA em `sync_error` com código
estável (`supplier_cost_changed`/`currency_changed`) — nunca aplicada ao
custo, à moeda ou à precificação; o administrador decide a renovação.

Transação única: esta API só faz `flush()`; o commit/rollback é da rota.
Nunca persiste nem loga token, app_secret, seller_id, product_id externo,
sku_id ou payload do fornecedor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from loja import db
from loja.modules.models import (
    Product,
    ProductSync,
    ProductVariant,
    SupplierProduct,
)
from loja.services.pricing import to_decimal
from loja.services.variant_attributes import (
    VARIANT_COLOR_MAX_LEN,
    VARIANT_LABEL_MAX_LEN,
    extract_variant_color_size,
)
from loja.utils import utcnow

from .importer import (
    CurrencyMismatchError,
    DraftImportError,
    _effective_supplier_price,
    _norm_currency,
    _priced_skus,
    _resolve_currency,
    _supplier_cost_basis,
)
from .models import AliExpressProductDetail

logger = logging.getLogger(__name__)

#: Códigos estáveis de divergência sinalizada (nunca aplicados).
DIVERGENCE_COST = "supplier_cost_changed"
DIVERGENCE_CURRENCY = "currency_changed"
DIVERGENCE_CODES = (DIVERGENCE_COST, DIVERGENCE_CURRENCY)


class PublishedSyncError(Exception):
    """Falha controlada da sincronização (mensagem é um código estável)."""


class PublishedSyncConflict(PublishedSyncError):
    """Conflito: operação impossível sem tocar em decisão comercial."""


@dataclass
class PublishedSyncResult:
    """Contagens e divergência de uma sincronização (para o log/flash)."""

    product_id: Optional[object] = None
    variants_updated: int = 0
    variants_deactivated: int = 0
    variants_created: int = 0
    stock_before: int = 0
    stock_after: int = 0
    divergence: Optional[str] = None


def sync_published_product(
    *,
    detail: AliExpressProductDetail,
    product: Product,
    supplier_product: SupplierProduct,
    now=None,
) -> PublishedSyncResult:
    """Sincroniza estoque/técnico de um produto PUBLICADO (somente leitura comercial).

    Levanta `PublishedSyncConflict`/`PublishedSyncError` com código estável ANTES
    de qualquer escrita quando a operação não pode ser feita com segurança; em
    caso de erro posterior o chamador deve dar rollback (aqui só há flush).
    """
    if product is None or supplier_product is None:
        raise PublishedSyncConflict("not_found")
    if product.is_draft or not product.is_active:
        # Rascunho usa import_product_draft; publicado nunca volta a rascunho.
        raise PublishedSyncConflict("not_published")
    if supplier_product.product_id != product.id:
        raise PublishedSyncConflict("link_mismatch")

    external_id = (
        str(detail.product_id) if detail.product_id is not None else None
    )
    if not external_id:
        raise PublishedSyncError("missing_product_id")
    if product.external_id and external_id != str(product.external_id):
        raise PublishedSyncConflict("external_id_mismatch")

    divergence = _observe_divergence(detail, supplier_product)
    now = now or utcnow()

    stock_before = int(product.stock or 0)
    updated, deactivated, created = _sync_variant_stocks(
        product, supplier_product, detail, now
    )
    stock_after = int(product.stock or 0)

    _mark_technical_sync(supplier_product, product, divergence, now)
    db.session.flush()

    result = PublishedSyncResult(
        product_id=product.id,
        variants_updated=updated,
        variants_deactivated=deactivated,
        variants_created=created,
        stock_before=stock_before,
        stock_after=stock_after,
        divergence=divergence,
    )
    logger.info(
        "published sync: variants_updated=%d variants_deactivated=%d "
        "variants_created=%d stock=%d->%d divergence=%s",
        result.variants_updated,
        result.variants_deactivated,
        result.variants_created,
        result.stock_before,
        result.stock_after,
        divergence or "none",
    )
    return result


# --------------------------------------------------------------------------
# divergência de custo/moeda (sinaliza, nunca aplica)
# --------------------------------------------------------------------------

def _observe_divergence(
    detail: AliExpressProductDetail, supplier_product: SupplierProduct
) -> Optional[str]:
    """Compara custo/moeda observados com os gravados. Nenhuma escrita."""
    priced = _priced_skus(detail.skus or [])
    if not priced:
        raise PublishedSyncError("missing_supplier_price")
    try:
        observed_currency = _resolve_currency(detail, priced)
    except CurrencyMismatchError:
        raise PublishedSyncError("currency_mismatch") from None
    except DraftImportError as exc:
        code = exc.args[0] if exc.args else "missing_currency"
        raise PublishedSyncError(str(code)) from None

    if _norm_currency(supplier_product.currency) != _norm_currency(
        observed_currency
    ):
        return DIVERGENCE_CURRENCY
    observed_cost = _supplier_cost_basis(priced)
    if (
        to_decimal(observed_cost) is not None
        and to_decimal(supplier_product.cost) != to_decimal(observed_cost)
    ):
        return DIVERGENCE_COST
    return None


# --------------------------------------------------------------------------
# estoque (única escrita de negócio desta API)
# --------------------------------------------------------------------------

def _sync_variant_stocks(
    product: Product,
    supplier_product: SupplierProduct,
    detail: AliExpressProductDetail,
    now,
) -> Tuple[int, int, int]:
    """Atualiza estoque das variantes; retorna (atualizadas, desativadas, criadas)."""
    existing = ProductVariant.query.filter_by(
        supplier_product_id=supplier_product.id
    ).all()
    by_supplier_sku = {v.supplier_sku_id: v for v in existing if v.supplier_sku_id}

    seen = set()
    updated = deactivated = created = 0
    for sku in detail.skus or []:
        if not sku.sku_id:
            continue
        sku_id = str(sku.sku_id)
        if sku_id in seen:
            continue
        seen.add(sku_id)
        stock = _stock_of(sku)
        variant = by_supplier_sku.get(sku_id)
        if variant is None:
            created += _create_inactive_variant(
                product, supplier_product, sku, sku_id, stock
            )
            continue

        # Só estoque: atributos/cor/tamanho/preço da variante são preservados
        # (mudança visível na vitrine exige decisão comercial explícita).
        if variant.stock != stock or (stock == 0 and variant.is_active):
            variant.stock = stock
            if stock == 0 and variant.is_active:
                variant.is_active = False
                deactivated += 1
            variant.updated_at = now
            updated += 1

    # SKU sumido do catálogo: deixa de estar à venda (nunca é deletado).
    for sku_id, variant in by_supplier_sku.items():
        if sku_id in seen:
            continue
        if variant.is_active or (variant.stock or 0):
            variant.stock = 0
            variant.is_active = False
            variant.updated_at = now
            updated += 1
            deactivated += 1

    active_stocks = [
        max(v.stock or 0, 0)
        for v in ProductVariant.query.filter_by(
            supplier_product_id=supplier_product.id, is_active=True
        ).all()
    ]
    product.stock = sum(active_stocks)
    return updated, deactivated, created


def _stock_of(sku) -> int:
    try:
        return max(int(sku.available_stock), 0)
    except (TypeError, ValueError):
        return 0


def _create_inactive_variant(
    product: Product,
    supplier_product: SupplierProduct,
    sku,
    sku_id: str,
    stock: int,
) -> int:
    """Novo SKU de produto publicado nasce INATIVO (nunca à venda sozinho)."""
    attributes = list(sku.attributes or [])
    color, size = extract_variant_color_size(attributes)
    for rotulo, teto in (
        (size, VARIANT_LABEL_MAX_LEN),
        (color, VARIANT_COLOR_MAX_LEN),
    ):
        if rotulo is not None and len(rotulo) > teto:
            raise PublishedSyncError("variant_label_too_long")
    db.session.add(
        ProductVariant(
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            external_sku_id=sku_id,
            supplier_sku_id=sku_id,
            sku=None,
            attributes=attributes or None,
            color=color,
            size=size,
            stock=stock,
            price=_effective_supplier_price(sku),
            sale_price=None,
            # decisão comercial: ativação de nova opção é explícita, não automática
            is_active=False,
        )
    )
    return 1


# --------------------------------------------------------------------------
# metadados técnicos de sincronização
# --------------------------------------------------------------------------

def _mark_technical_sync(
    supplier_product: SupplierProduct,
    product: Product,
    divergence: Optional[str],
    now,
) -> None:
    """Carimbo de sincronização técnica + divergência sinalizada.

    NÃO escreve em cost/currency/price/pricing_status/is_active/is_draft.
    """
    supplier_product.sync_status = "synced"
    supplier_product.last_sync_at = now
    supplier_product.sync_error = divergence
    product.sync_status = "synced"
    product.last_sync_at = now
    product.sync_error = divergence

    product_sync = ProductSync.query.filter_by(
        product_id=product.id, supplier_id=supplier_product.supplier_id
    ).first()
    if product_sync is None:
        db.session.add(
            ProductSync(
                product_id=product.id,
                supplier_id=supplier_product.supplier_id,
                external_product_id=product.external_id,
                last_synced_at=now,
                error=divergence,
            )
        )
        return
    product_sync.last_synced_at = now
    product_sync.error = divergence
    # `state` preservado: sincronização de publicado nunca volta a "draft".


__all__ = [
    "DIVERGENCE_COST",
    "DIVERGENCE_CURRENCY",
    "DIVERGENCE_CODES",
    "PublishedSyncError",
    "PublishedSyncConflict",
    "PublishedSyncResult",
    "sync_published_product",
]
