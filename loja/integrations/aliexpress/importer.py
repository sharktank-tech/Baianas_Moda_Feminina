"""Import idempotente de produto AliExpress como rascunho (Fase 5).

Transforma um `AliExpressProductDetail` normalizado (read-only) na representação
genérica da PolloModas, SEMPRE como draft:

    Product(source="aliexpress", is_draft=True, is_active=False)
    Supplier(channel_type="aliexpress", seller_id=<seller>)
    SupplierProduct(anchor supplier_id+supplier_product_id)
    ProductVariant[ols] (por supplier_sku_id)
    ProductImage[s] (galeria deduplicada, ordenada)
    ProductSync (upsert por product_id+supplier_id)

Regras invariantes desta fase:
  * Nunca calcula margem/FX/frete. Frete fica `provider_unavailable` com
    shipping_cost/landed_cost/freight_currency = NULL (ausência NUNCA é zero).
    Um reimport NÃO sobrescreve uma confirmação de frete/landed existente.
  * Fase 6: todo novo draft nasce `blocked` com pricing_error
    'freight_unavailable' e product.currency = moeda resolvida. Produto já
    `priced` NÃO é rebaixado nem tem o preço de venda sobrescrito.
  * Reimport com mudança MATERIAL de custo/moeda do fornecedor invalida
    `priced` -> `stale` (supplier_cost_changed ou currency_changed) e NUNCA
    reprecifica automaticamente; preço/margem preservados para auditoria.
    Reimports sucessivos de um produto já `stale` continuam reconciliando o
    landed com o custo novo (ou NULL quando frete/moeda divergirem).
  * Fase 7.1 (multivariantes — custo seguro): separa o custo do fornecedor do
    preço de exibição do draft. `supplier_cost_basis` = MAIOR preço positivo
    entre os SKUs ATIVOS (é a base de custo para landed/pricing; `SupplierProduct.cost`
    recebe esse valore `Product.cost` o acompanha). `catalog_price` = MENOR
    preço positivo entre os SKUs, usado apenas como preço provisório/display de
    `Product.price` enquanto `pricing_status != 'priced'`. Nenhuma variante ativa
    pode ter custo superior ao cost basis (defesa abaixo).
  * Moeda única entre os SKUs com preço; divergência aborta (sem conversão).
  * Idempotente: reimport não duplica Supplier/Product/Variante/Imagem/Sync.
  * Transação única atômica; qualquer falha faz rollback total.
  * NUNCA loga seller_id, product_id externo, sku_id, raw payload, code/state,
    token, app_secret ou sign. Logs apenas operacionais (contagens).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from loja import db
from loja.modules.models import (
    Product,
    ProductImage,
    ProductSync,
    ProductVariant,
    Supplier,
    SupplierCategoryMapping,
    SupplierProduct,
)
from loja.services.pricing import (
    BLOCKED,
    PRICED,
    STALE,
    PricingBlocked,
    calculate_landed_cost,
    to_decimal,
)
from loja.services.variant_attributes import (
    COLOR_NAMES,
    SIZE_NAMES,
    VARIANT_COLOR_MAX_LEN,
    VARIANT_LABEL_MAX_LEN,
    extract_variant_color_size,
)
from loja.utils import utcnow

from .models import AliExpressProductDetail, AliExpressProductSku

logger = logging.getLogger(__name__)

SOURCE_ALIEXPRESS = "aliexpress"
DRAFT_STATE = "draft"
FREIGHT_PROVIDER_UNAVAILABLE = "provider_unavailable"
FREIGHT_AVAILABLE = "available"


class DraftImportError(Exception):
    """Falha controlada do import de rascunho (mensagem é um código estável)."""


class DraftImportConflict(DraftImportError):
    """Conflito que impede o import (nunca sobrescreve decisões comerciais)."""


class CurrencyMismatchError(DraftImportError):
    """Mais de uma moeda observada entre os SKUs com preço."""


@dataclass
class DraftImportResult:
    product_id: UUID
    supplier_product_id: UUID
    created: bool
    variants_created: int = 0
    variants_updated: int = 0
    variants_disabled: int = 0
    images: int = 0


@dataclass
class _VariantCounts:
    created: int = 0
    updated: int = 0
    disabled: int = 0


def import_product_draft(
    *,
    detail: AliExpressProductDetail,
    seller_id: str,
) -> DraftImportResult:
    """Importa (ou reimporta) um produto AliExpress como rascunho idempotente.

    Opera em uma única transação: um único commit ao final; rollback completo
    em qualquer aborte/erro (inclusive IntegrityError, convertido em conflito
    sanitizado). Nunca persiste token/secret/code/state/sign.
    """
    external_id = str(detail.product_id) if detail.product_id is not None else None
    if not external_id:
        raise DraftImportError("missing_product_id")

    priced = _priced_skus(detail.skus or [])
    if not priced:
        raise DraftImportError("missing_supplier_price")
    currency = _resolve_currency(detail, priced)
    # Menu de preços do fornecedor: menor = provisório/display; maior = base de
    # custo segura (SupplierProduct.cost / Product.cost) para landed e pricing.
    catalog_price = _catalog_price(priced)
    supplier_cost_basis = _supplier_cost_basis(priced)
    now = utcnow()

    try:
        supplier = _get_or_create_supplier(str(seller_id), currency)
        product, supplier_product, created = _resolve_product_and_link(
            supplier=supplier,
            external_id=external_id,
            detail=detail,
            currency=currency,
            catalog_price=catalog_price,
            supplier_cost_basis=supplier_cost_basis,
            now=now,
        )

        _apply_category(product, supplier, detail, created)
        _apply_product(product, detail, catalog_price, supplier_cost_basis, currency, now)
        variant_counts, images = _apply_variants_and_images(
            product, supplier_product, detail, now
        )
        _upsert_product_sync(product, supplier, external_id, now)

        db.session.flush()
        result = DraftImportResult(
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            created=created,
            variants_created=variant_counts.created,
            variants_updated=variant_counts.updated,
            variants_disabled=variant_counts.disabled,
            images=images,
        )
        db.session.commit()
        logger.info(
            "draft import: created=%s variants_created=%d variants_updated=%d "
            "variants_disabled=%d images=%d",
            "yes" if created else "no",
            variant_counts.created,
            variant_counts.updated,
            variant_counts.disabled,
            images,
        )
        return result
    except IntegrityError:
        db.session.rollback()
        raise DraftImportConflict("conflict") from None
    except DraftImportError:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        raise


# --------------------------------------------------------------------------
# preço / moeda
# --------------------------------------------------------------------------

def _effective_supplier_price(sku: AliExpressProductSku) -> Optional[Decimal]:
    value = sku.sale_price if sku.sale_price is not None else sku.original_price
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            return None
    return value


def _priced_skus(skus: List[AliExpressProductSku]) -> List[AliExpressProductSku]:
    return [sku for sku in skus if _effective_supplier_price(sku) is not None]


def _sku_is_active(sku: AliExpressProductSku) -> bool:
    stock = sku.available_stock
    if stock is None:
        return True
    try:
        return int(stock) > 0
    except (TypeError, ValueError):
        return True


def _sku_positives(priced: List[AliExpressProductSku]) -> List[Decimal]:
    return [
        price
        for price in (_effective_supplier_price(sku) for sku in priced)
        if price is not None and price > 0
    ]


def _catalog_price(priced: List[AliExpressProductSku]):
    """MENOR preço positivo entre os SKUs — provisório/display (não publicável).

    Usado apenas como Product.price provisório enquanto `pricing_status != priced`
    (o preço de venda canônico vem de price_dropshipping_product()).
    """
    positive = _sku_positives(priced)
    if not positive:
        raise DraftImportError("missing_supplier_price")
    return min(positive)


def _supplier_cost_basis(priced: List[AliExpressProductSku]):
    """MAIOR preço efetivo positivo entre os SKUs ATIVOS — base de custo segura.

    Custo conservador (pior caso) para dismantling seguro do landed/pricing:
    repor o produto inteiro custaria o SKU mais caro ainda vendável (ativo).
    """
    active_positive = [
        price
        for sku in priced
        for price in [_effective_supplier_price(sku)]
        if price is not None and price > 0 and _sku_is_active(sku)
    ]
    if not active_positive:
        # sem SKU ativo com preço: cai para o menor preço positivo observado
        # (defensivo; mantém o import não-falho mesmo em dados incompletos).
        fallback = _sku_positives(priced)
        return min(fallback) if fallback else None
    return max(active_positive)


def _norm_currency(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _resolve_currency(
    detail: AliExpressProductDetail, priced: List[AliExpressProductSku]
) -> str:
    currencies = {
        _norm_currency(sku.currency)
        for sku in priced
        if _norm_currency(sku.currency)
    }
    if len(currencies) > 1:
        raise CurrencyMismatchError("currency_mismatch")
    if len(currencies) == 1:
        return currencies.pop()
    fallback = _norm_currency(detail.currency)
    if fallback:
        return fallback
    raise DraftImportError("missing_currency")


# --------------------------------------------------------------------------
# fornecedor / produto / vínculo
# --------------------------------------------------------------------------

def _get_or_create_supplier(seller_id: str, currency: str) -> Supplier:
    supplier = Supplier.query.filter_by(
        channel_type="aliexpress", seller_id=seller_id
    ).first()
    if supplier is not None:
        return supplier
    supplier = Supplier(
        name="AliExpress",
        channel_type="aliexpress",
        seller_id=seller_id,
        default_currency=currency,
        is_sync_enabled=False,
    )
    db.session.add(supplier)
    db.session.flush()
    return supplier


def _resolve_product_and_link(
    *,
    supplier: Supplier,
    external_id: str,
    detail: AliExpressProductDetail,
    currency: str,
    catalog_price,
    supplier_cost_basis,
    now,
) -> Tuple[Product, SupplierProduct, bool]:
    existing_sp = SupplierProduct.query.filter_by(
        supplier_id=supplier.id, supplier_product_id=external_id
    ).first()
    if existing_sp is not None:
        product = existing_sp.product
        # Mesmo com vínculo existente: produto publicado NUNCA volta a ser
        # rascunho (decisões comerciais não são sobrescritas nesta fase).
        if not product.is_draft or product.is_active:
            raise DraftImportConflict("already_published")
        _apply_supplier_product(
            existing_sp,
            product,
            catalog_price,
            supplier_cost_basis,
            currency,
            now,
        )
        return product, existing_sp, False

    matches = Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=external_id
    ).all()
    if len(matches) > 1:
        raise DraftImportConflict("duplicate_aliexpress_product")
    if matches:
        product = matches[0]
        created = False
    else:
        product = Product(
            name=(detail.title or "").strip() or "Produto AliExpress",
            description=detail.description,
            price=catalog_price,
            source=SOURCE_ALIEXPRESS,
            external_id=external_id,
            is_draft=True,
            is_active=False,
        )
        db.session.add(product)
        created = True

    # Produto AliExpress já publicado: conflito controlado, nunca vira draft
    # silenciosamente (decisões comerciais não são sobrescritas nesta fase).
    if not created and (not product.is_draft or product.is_active):
        raise DraftImportConflict("already_published")

    db.session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        product_id=product.id,
        supplier_product_id=external_id,
    )
    db.session.add(supplier_product)
    _apply_supplier_product(
        supplier_product, product, catalog_price, supplier_cost_basis, currency, now
    )
    db.session.flush()
    return product, supplier_product, created


def _apply_supplier_product(
    supplier_product: SupplierProduct,
    product: Product,
    catalog_price,
    supplier_cost_basis,
    currency: str,
    now,
) -> None:
    previous_cost = supplier_product.cost
    previous_currency = supplier_product.currency

    # Fase 7.1: SupplierProduct.cost = MAIOR preço efetivo positivo entre SKUs
    # ativos (base de custo segura), e Product.cost acompanha esse mesmo basis.
    supplier_product.cost = supplier_cost_basis
    product.cost = supplier_cost_basis
    supplier_product.wholesale_price = None
    supplier_product.currency = currency
    supplier_product.sync_status = "synced"
    supplier_product.last_sync_at = now
    supplier_product.sync_error = None
    supplier_product.is_active = True

    if not (
        supplier_product.freight_status == FREIGHT_AVAILABLE
        and to_decimal(supplier_product.shipping_cost) is not None
    ):
        # Sem frete: provider_unavailable -> custos de frete indefinidos
        # (NULL), NUNCA zero. Um reimport NÃO sobrescreve uma confirmação de
        # frete existente; a reconciliação abaixo recalcula o landed apenas
        # quando há mudança material de custo.
        supplier_product.freight_status = FREIGHT_PROVIDER_UNAVAILABLE
        supplier_product.shipping_cost = None
        supplier_product.landed_cost = None
        supplier_product.freight_currency = None

    _reconcile_pricing_on_supplier_change(
        product=product,
        supplier_product=supplier_product,
        previous_cost=previous_cost,
        previous_currency=previous_currency,
    )


def _reconcile_pricing_on_supplier_change(
    *,
    product: Product,
    supplier_product: SupplierProduct,
    previous_cost,
    previous_currency,
) -> None:
    """Reconciliação de pricing após atualização de custo/moeda (Fase 6).

    Aplica-se a produtos `priced` E `stale` (reimports sucessivos continuam
    reconciliando o landed sem reprecificar):
      * custo E moeda inalterados   -> nenhuma mudança (mantém estado atual,
        inclusive `priced`).
      * moeda do fornecedor mudou   -> `stale`/currency_changed e landed cost
        NULL (moeda é moeda; não existe conversão/FX).
      * custo mudou (moeda igual)   -> `stale`/supplier_cost_changed. O landed
        é recalculado reutilizando calculate_landed_cost(): frete utilizável
        => landed = custo_novo + frete; caso contrário landed NULL.

    NUNCA reprecifica: Product.price e target_margin_percent ficam preservados
    para auditoria; `stale` impede publicação até uma nova chamada explícita de
    price_dropshipping_product().
    """
    if product.pricing_status not in (PRICED, STALE):
        return

    cost_before = to_decimal(previous_cost)
    cost_after = to_decimal(supplier_product.cost)
    currency_before = _norm_currency(previous_currency)
    currency_after = _norm_currency(supplier_product.currency)

    if cost_before == cost_after and currency_before == currency_after:
        return

    if currency_before != currency_after:
        _mark_pricing_stale(product, "currency_changed")
        supplier_product.landed_cost = None
        return

    try:
        supplier_product.landed_cost = calculate_landed_cost(supplier_product)
    except PricingBlocked as exc:
        code = exc.args[0] if exc.args else "unknown"
        supplier_product.landed_cost = None
        if code == "currency_mismatch":
            # Incoerência de moeda (fornecedor × frete): irrecuperável sem FX.
            _mark_pricing_stale(product, "currency_changed")
        else:
            # Frete deixou de ser utilizável (ou custo agora inválido): landed
            # fica NULL, mas a causa continua sendo a mudança de custo.
            _mark_pricing_stale(product, "supplier_cost_changed")
        return

    _mark_pricing_stale(product, "supplier_cost_changed")


def _mark_pricing_stale(product: Product, error_code: str) -> None:
    product.pricing_status = STALE
    product.pricing_error = error_code
    logger.info("pricing: stale reason=%s", error_code)


def _apply_category(
    product: Product, supplier: Supplier, detail: AliExpressProductDetail, created: bool
) -> None:
    if not detail.category_id:
        return
    mapping = SupplierCategoryMapping.query.filter_by(
        supplier_id=supplier.id,
        external_category_id=str(detail.category_id),
    ).first()
    if mapping is not None and mapping.category_id is not None:
        product.category_id = mapping.category_id
        if mapping.category is not None:
            product.category = mapping.category.name
    elif created:
        # Sem mapping conhecido: novo draft começa SEM categoria manual.
        product.category_id = None
        product.category = None


def _apply_product(
    product: Product,
    detail: AliExpressProductDetail,
    catalog_price,
    supplier_cost_basis,
    currency: str,
    now,
) -> None:
    product.name = (detail.title or "").strip() or "Produto AliExpress"
    product.description = detail.description
    product.source = SOURCE_ALIEXPRESS
    product.external_id = str(detail.product_id)
    # Fase 7.1: Product.cost acompanha o cost basis do fornecedor (não é fonte
    # canônica de pricing, mas evita informação administrativa enganosa). O
    # preço provisório de display é o MENOR preço (catalog_price).
    product.cost = supplier_cost_basis
    product.wholesale_price = None
    product.is_draft = True
    product.is_active = False
    product.sync_status = "synced"
    product.last_sync_at = now
    product.sync_error = None
    # Fase 6: moeda de VENDA = moeda resolvida do fornecedor (sem camada FX;
    # Product.currency/SupplierProduct.currency devem ser iguais para pricing).
    # Para produtos já PRECIFICADOS (priced) ou INVALIDADOS (stale) o reimport
    # NÃO altera automaticamente Product.currency/price/target_margin_percent:
    # esses três valores formam o ÚLTIMO snapshot comercial precificado (p.ex.
    # em USD) e uma moeda de venda nova é adoção EXPLÍCITA do vendedor, nunca
    # consequência silenciosa do catálogo (senão o mesmo valor de price passaria
    # a valer em EUR sem FX/repricing). A divergência Product.currency !=
    # SupplierProduct.currency fica deliberada enquanto `stale`, mantendo
    # price_dropshipping_product()/evaluate_publication() bloqueados por
    # currency_mismatch. Drafts que nunca foram precificados continuam adotando
    # a moeda resolvida do fornecedor e nascem `blocked`/freight_unavailable.
    if product.pricing_status not in (PRICED, STALE):
        product.currency = currency
        product.price = catalog_price
        product.pricing_status = BLOCKED
        product.pricing_error = "freight_unavailable"


# --------------------------------------------------------------------------
# variantes / imagens / sync
# --------------------------------------------------------------------------

def _extract_variant_meta(
    attributes: Optional[List[dict]],
) -> Tuple[Optional[str], Optional[str]]:
    # Delegação ao helper compartilhado (nomes + sku_property_id); mantém
    # primeira ocorrência por dimensão e nunca presume dimensão desconhecida.
    return extract_variant_color_size(attributes)


def _apply_variants_and_images(
    product: Product,
    supplier_product: SupplierProduct,
    detail: AliExpressProductDetail,
    now,
) -> Tuple[_VariantCounts, int]:
    existing = ProductVariant.query.filter_by(
        supplier_product_id=supplier_product.id
    ).all()
    by_supplier_sku = {v.supplier_sku_id: v for v in existing if v.supplier_sku_id}

    seen = set()
    variant_counts = _VariantCounts()
    for sku in detail.skus or []:
        if not sku.sku_id:
            continue
        sku_id = str(sku.sku_id)
        if sku_id in seen:
            # sku_id duplicado no MESMO payload: primeira ocorrência vence; a
            # constraint uq_product_variant_supplier impede a segunda row.
            continue
        seen.add(sku_id)
        attributes = list(sku.attributes or [])
        color, size = _extract_variant_meta(attributes)
        # Rótulos acima do teto de cada coluna (size: 40, cor: 60) abortam a
        # importação com código estável e rollback total: nunca truncar nem
        # persistir parcial (valores existentes preservados por não haver
        # escrita).
        for rotulo, teto in (
            (size, VARIANT_LABEL_MAX_LEN),
            (color, VARIANT_COLOR_MAX_LEN),
        ):
            if rotulo is not None and len(rotulo) > teto:
                raise DraftImportError("variant_label_too_long")
        stock = max(int(sku.available_stock) if sku.available_stock is not None else 0, 0)
        price = _effective_supplier_price(sku)
        variant = by_supplier_sku.get(sku_id)
        if variant is None:
            db.session.add(
                ProductVariant(
                    product_id=product.id,
                    supplier_product_id=supplier_product.id,
                    external_sku_id=sku_id,
                    supplier_sku_id=sku_id,
                    sku=None,  # identidade = sku_id normalizado; sem inventar sku_code/barcode
                    attributes=attributes or None,
                    color=color,
                    size=size,
                    stock=stock,
                    price=price,
                    sale_price=None,
                    is_active=stock > 0,
                )
            )
            variant_counts.created += 1
        else:
            variant.attributes = attributes or None
            variant.color = color
            variant.size = size
            variant.stock = stock
            variant.price = price
            variant.sale_price = None
            variant.is_active = stock > 0
            variant.updated_at = now
            variant_counts.updated += 1

    # SKU removido: nunca DELETA; desativa (stock=0, is_active=False).
    for sku_id, variant in by_supplier_sku.items():
        if sku_id in seen:
            continue
        if variant.is_active or variant.stock:
            variant.stock = 0
            variant.is_active = False
            variant.updated_at = now
            variant_counts.disabled += 1

    # Estoque agregado do produto = soma dos estoques das variantes ATIVAS
    # deste fornecedor (desconhecido tratado como 0).
    active_stocks = [
        max(v.stock or 0, 0)
        for v in ProductVariant.query.filter_by(
            supplier_product_id=supplier_product.id, is_active=True
        ).all()
    ]
    product.stock = sum(active_stocks)

    image_count = _apply_images(product, detail)
    return variant_counts, image_count


def _normalize_images(urls: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for url in urls or []:
        if not isinstance(url, str):
            continue
        cleaned = url.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _apply_images(product: Product, detail: AliExpressProductDetail) -> int:
    urls = _normalize_images(detail.images or [])
    product.image1 = urls[0] if len(urls) > 0 else None
    product.image2 = urls[1] if len(urls) > 1 else None
    product.image3 = urls[2] if len(urls) > 2 else None
    product.image4 = urls[3] if len(urls) > 3 else None

    # Recria apenas a galeria DESTE produto (nunca de outros) de forma
    # deduplicada e em ordem; reimport não gera duplicatas.
    ProductImage.query.filter_by(product_id=product.id).delete(
        synchronize_session=False
    )
    alt = (detail.title or "").strip() or "Produto AliExpress"
    gallery = [
        ProductImage(
            product_id=product.id,
            url=url,
            alt=alt,
            position=position,
            is_primary=position == 0,
        )
        for position, url in enumerate(urls)
    ]
    db.session.add_all(gallery)
    return len(gallery)


def _upsert_product_sync(
    product: Product,
    supplier: Supplier,
    external_id: str,
    now,
) -> None:
    product_sync = ProductSync.query.filter_by(
        product_id=product.id, supplier_id=supplier.id
    ).first()
    if product_sync is None:
        db.session.add(
            ProductSync(
                product_id=product.id,
                supplier_id=supplier.id,
                external_product_id=external_id,
                state=DRAFT_STATE,
                last_synced_at=now,
                error=None,
            )
        )
    else:
        product_sync.external_product_id = external_id
        product_sync.state = DRAFT_STATE
        product_sync.last_synced_at = now
        product_sync.error = None


__all__ = [
    "DraftImportError",
    "DraftImportConflict",
    "CurrencyMismatchError",
    "DraftImportResult",
    "import_product_draft",
    "SOURCE_ALIEXPRESS",
    "DRAFT_STATE",
    "FREIGHT_PROVIDER_UNAVAILABLE",
    "FREIGHT_AVAILABLE",
    "COLOR_NAMES",
    "SIZE_NAMES",
]