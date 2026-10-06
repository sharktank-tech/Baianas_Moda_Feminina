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
  * `price`, `target_margin_percent`, `currency`, `cost`
    (divergência deixa a precificação STALE, mas preço/margem/conteúdo
    permanecem intactos até o administrador renovar);
  * `is_draft`/`is_active` (NUNCA despublica), frete/landed/evidência;
  * origem de envio e seleção de operação nacional;
  * atributos/cor/tamanho/preço de variante já existentes, e reativação de
    variante desativada (o produto nunca ganha venda nova por sincronização).

DIVERGÊNCIA DE CUSTO/MOEDA: comparada e SINALIZADA em `sync_error` com código
estável (`supplier_cost_changed`/`currency_changed`) — nunca aplicada ao
custo, à moeda, ao preço ou à margem — e INVALIDA a aprovação operacional:
`pricing_status` vira `stale` (guarda de banco na migração 0035 permite
`stale` em produto publicado). Enquanto durar, os controles canônicos de
compra (`loja.services.purchase_controls`) impedem nova venda e a
publicação/republicação segue bloqueada; o administrador resolve
renovando/reprecificando.

CONCORRÊNCIA: as variantes são travadas (`SELECT ... FOR UPDATE`) na MESMA
ordem do checkout (Product -> ProductVariant) e os objetos JÁ CARREGADOS
são recarregados sob o lock (`populate_existing` + refresh do vínculo):
o `FOR UPDATE` atualiza o banco, não o identity map, e decidir com o
snapshot anterior ao lock gravaria por cima de um checkout que já
commitou. O valor absoluto aplicado é
`estoque do fornecedor - reservas locais ainda debitadas`
(`stock_reservations.reserved_quantities_by_variant`), para nunca devolver
estoque que um pedido pendente já consumiu, e o total informado pelo
fornecedor fica gravado em `ProductVariant.supplier_stock` — a liberação
de reserva usa esse valor como TETO e nunca devolve acima dele quando o
fornecedor reduziu o estoque.

CATÁLOGO COMPLETO: SKUs ausentes só são desativados quando a resposta PROVA
completude (contagem agregada declarada confere com o observado). A simples
presença de SKUs nunca prova completude; sem prova, os SKUs presentes são
atualizados e nada é desativado (`catalog_code` no resultado).

Transação única: esta API só faz `flush()`; o commit/rollback é da rota.
Nunca persiste nem loga token, app_secret, seller_id, product_id externo,
sku_id ou payload do fornecedor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from sqlalchemy import inspect

from loja import db
from loja.modules.models import (
    Product,
    ProductSync,
    ProductVariant,
    SupplierProduct,
)
from loja.services.pricing import STALE, to_decimal
from loja.services.stock_reservations import reserved_quantities_by_variant
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

#: Códigos de completude não provada do catálogo (fail-closed).
CATALOG_CENSUS_EMPTY = "catalog_census_empty"
CATALOG_CENSUS_UNPROVEN = "catalog_census_unproven"
CATALOG_COUNT_MISMATCH = "catalog_sku_count_mismatch"
CATALOG_STOCK_MISMATCH = "catalog_stock_mismatch"


class PublishedSyncError(Exception):
    """Falha controlada da sincronização (mensagem é um código estável)."""


class PublishedSyncConflict(PublishedSyncError):
    """Conflito: operação impossível sem tocar em decisão comercial."""


@dataclass
class PublishedSyncResult:
    """Contagens, divergência e completude de uma sincronização."""

    product_id: Optional[object] = None
    variants_updated: int = 0
    variants_deactivated: int = 0
    variants_created: int = 0
    stock_before: int = 0
    stock_after: int = 0
    divergence: Optional[str] = None
    #: True só quando a resposta PROVOU completude do catálogo.
    catalog_complete: bool = True
    #: Código estável de completude não provada (None quando comprovada).
    catalog_code: Optional[str] = None


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

    # Trava NA ORDEM do checkout (Product -> ProductVariant) e RECARREGA
    # os registros já carregados ANTES de ler qualquer valor de negócio.
    # `with_for_update()` não atualiza instâncias do identity map e o
    # resultado da consulta era descartado: sem este reload a sync decidia
    # com o snapshot lido ANTES dos locks e gravava por cima do que um
    # checkout que commitou no intervalo escreveu.
    locked_variants = _lock_and_reload(product, supplier_product)

    divergence = _observe_divergence(detail, supplier_product)
    census = catalog_census(detail)
    now = now or utcnow()

    stock_before = int(product.stock or 0)
    updated, deactivated, created = _sync_variant_stocks(
        product, supplier_product, detail, now, census, locked_variants
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
        catalog_complete=census.complete,
        catalog_code=census.code,
    )
    logger.info(
        "published sync: variants_updated=%d variants_deactivated=%d "
        "variants_created=%d stock=%d->%d divergence=%s catalog=%s",
        result.variants_updated,
        result.variants_deactivated,
        result.variants_created,
        result.stock_before,
        result.stock_after,
        divergence or "none",
        "complete" if census.complete else str(census.code),
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
# completude do catálogo (nunca presumida pela presença de SKUs)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CatalogCensus:
    """Resultado do censo de completude da resposta do fornecedor."""

    complete: bool
    code: Optional[str] = None
    declared: Optional[int] = None
    observed: int = 0


def _declared_sku_total(detail: AliExpressProductDetail) -> Optional[int]:
    """Contagem de SKUs DECLARADA pela resposta (None quando não declarada)."""
    raw = getattr(detail, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    container = raw.get("ae_item_sku_info_dtos")
    base = raw.get("ae_item_base_info_dto")

    # Cada fonte só é lida com as chaves que significam contagem de SKUs
    # NAQUELA fonte: nome genérico ("total"/"count") só vale DENTRO do
    # container de SKUs, onde não pode significar outra coisa.
    # `ae_item_base_info_dto.total` é total de itens/estoque do anúncio,
    # NUNCA quantidade de SKUs — parear por índice aceitaria esse "total"
    # como contagem e provaria completude falsa quando o container está
    # ausente.
    container_keys = ("sku_count", "total_skus", "sku_total", "total", "count")
    count_keys = ("sku_count", "total_skus", "sku_total")
    candidates = []
    if isinstance(container, Mapping):
        candidates.append((container, container_keys))
    if isinstance(base, Mapping):
        candidates.append((base, count_keys))
    candidates.append((raw, count_keys))
    for source, keys in candidates:
        for key in keys:
            if key not in source:
                continue
            try:
                return int(source[key])
            except (TypeError, ValueError):
                continue
    return None


def _declared_stock_total(detail: AliExpressProductDetail) -> Optional[int]:
    """Estoque agregado DECLARADO pela resposta (None quando não declarado)."""
    value = getattr(detail, "available_stock", None)
    if value is None:
        raw = getattr(detail, "raw", None)
        base = raw.get("ae_item_base_info_dto") if isinstance(raw, Mapping) else None
        value = base.get("available_stock") if isinstance(base, Mapping) else None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def catalog_census(detail: AliExpressProductDetail) -> CatalogCensus:
    """Completude PROVADA do catálogo recebido (fail-closed).

    A presença de SKUs nunca prova completude (a resposta pode trazer só
    parte do catálogo). Só a contagem ou o agregado DECLARADOS pela
    resposta, conferindo com o observado, autorizam desativar SKUs
    ausentes. Sem prova, os SKUs presentes continuam atualizando normal e
    nada é desativado.
    """
    seen = set()
    observed_stock = 0
    for sku in detail.skus or []:
        if not sku.sku_id:
            continue
        sku_id = str(sku.sku_id)
        if sku_id in seen:
            continue
        seen.add(sku_id)
        observed_stock += _stock_of(sku)

    observed = len(seen)
    if observed == 0:
        # Resposta sem nenhum SKU nunca prova "catálogo vazio".
        return CatalogCensus(False, CATALOG_CENSUS_EMPTY, None, 0)

    declared_count = _declared_sku_total(detail)
    if declared_count is not None:
        if declared_count == observed:
            return CatalogCensus(True, None, declared_count, observed)
        return CatalogCensus(
            False, CATALOG_COUNT_MISMATCH, declared_count, observed
        )

    declared_stock = _declared_stock_total(detail)
    if declared_stock is not None:
        if declared_stock == observed_stock:
            return CatalogCensus(True, None, declared_stock, observed)
        return CatalogCensus(
            False, CATALOG_STOCK_MISMATCH, declared_stock, observed
        )

    return CatalogCensus(False, CATALOG_CENSUS_UNPROVEN, None, observed)


# --------------------------------------------------------------------------
# estoque (única escrita de negócio desta API)
# --------------------------------------------------------------------------

def _locked_variants(product: Product, supplier_product: SupplierProduct):
    """Trava Product -> ProductVariant na MESMA ordem usada no checkout.

    Devolve ``(product, variantes)``. ``populate_existing()`` é obrigatório:
    sem ele a instância já existente no identity map mantém o snapshot
    anterior ao lock (o ``FOR UPDATE`` atualiza o BANCO, não o objeto em
    memória), e o resultado da consulta seria um descarte silencioso.
    """
    locked_product = (
        Product.query
        .filter_by(id=product.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    variants = (
        ProductVariant.query
        .filter_by(supplier_product_id=supplier_product.id)
        .with_for_update()
        .populate_existing()
        .order_by(ProductVariant.id)
        .all()
    )
    return locked_product, variants


def _lock_and_reload(product: Product, supplier_product: SupplierProduct):
    """Trava os registros na ordem do checkout e os RECARREGA no identity map.

    Devolve as variantes travadas. O vínculo (SupplierProduct) também é
    relido, porque a comparação de divergência de custo/moeda lê o custo
    gravado — uma reprecificação que commitou depois do load não pode ser
    comparada contra um valor velho.
    """
    locked_product, variants = _locked_variants(product, supplier_product)
    if locked_product is None:
        # Linha removida concorrentemente: conflito, nunca sync às cegas.
        raise PublishedSyncConflict("not_found")
    if inspect(supplier_product).persistent:
        db.session.refresh(supplier_product)
    return variants


def _sync_variant_stocks(
    product: Product,
    supplier_product: SupplierProduct,
    detail: AliExpressProductDetail,
    now,
    census: CatalogCensus,
    variants=None,
) -> Tuple[int, int, int]:
    """Atualiza estoque das variantes; retorna (atualizadas, desativadas, criadas).

    O valor absoluto aplicado é sempre
    ``estoque do fornecedor - reservas locais ainda debitadas``: um
    pedido pendente nunca tem o estoque que ele já consumiu devolvido por
    uma sincronização. SKU ausente só é desativado quando o censo provou a
    completude da resposta.
    """
    if variants is None:
        variants = _lock_and_reload(product, supplier_product)
    reserved = reserved_quantities_by_variant([v.id for v in variants])
    by_supplier_sku = {v.supplier_sku_id: v for v in variants if v.supplier_sku_id}

    seen = set()
    updated = deactivated = created = 0
    for sku in detail.skus or []:
        if not sku.sku_id:
            continue
        sku_id = str(sku.sku_id)
        if sku_id in seen:
            continue
        seen.add(sku_id)
        variant = by_supplier_sku.get(sku_id)
        observed = _stock_of(sku)
        if variant is None:
            created += _create_inactive_variant(
                product, supplier_product, sku, sku_id, observed
            )
            continue

        # Fornecedor reporta o TOTAL disponível; a reserva local ainda
        # debitada (pedido pendente) segue debitada — nunca é devolvida.
        stock = max(observed - reserved.get(variant.id, 0), 0)

        # Espelho do total INFORMADO pelo fornecedor: é o teto que a
        # liberação de reserva usa para nunca devolver além dele. Conta
        # como alteração só a mudança de estoque/venda (a liberação não
        # infla acima desta coluna quando o fornecedor reduziu o estoque).
        variant.supplier_stock = observed

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
    # Exige completude comprovada — resposta parcial não desativa nada.
    if census.complete:
        for sku_id, variant in by_supplier_sku.items():
            if sku_id in seen:
                continue
            # Completude comprovada e SKU ausente: o fornecedor não tem a
            # opção — o teto de liberação passa a ser zero.
            variant.supplier_stock = 0
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
            supplier_stock=stock,
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

    NÃO escreve em cost/currency/price/target_margin/is_active/is_draft.
    Divergência de custo/moeda INVALIDA a aprovação operacional: a
    precificação passa a `stale` com o código estável em `pricing_error`,
    sem alterar preço, margem ou conteúdo (migração 0005/0035 aceita
    `stale` em produto publicado; os controles canônicos de compra
    bloqueiam a venda enquanto durar).
    """
    supplier_product.sync_status = "synced"
    supplier_product.last_sync_at = now
    supplier_product.sync_error = divergence
    product.sync_status = "synced"
    product.last_sync_at = now
    product.sync_error = divergence
    if divergence:
        product.pricing_status = STALE
        product.pricing_error = divergence

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
    "CATALOG_CENSUS_EMPTY",
    "CATALOG_CENSUS_UNPROVEN",
    "CATALOG_COUNT_MISMATCH",
    "CATALOG_STOCK_MISMATCH",
    "DIVERGENCE_COST",
    "DIVERGENCE_CURRENCY",
    "DIVERGENCE_CODES",
    "CatalogCensus",
    "PublishedSyncError",
    "PublishedSyncConflict",
    "PublishedSyncResult",
    "catalog_census",
    "sync_published_product",
]
