"""Testes do import idempotente de rascunho AliExpress (Fase 5).

Cobertura (lista obrigatória):
- 1º import: 1 Supplier/1 Product/1 SupplierProduct/N variantes/N imagens
  deduplicadas/1 ProductSync
- 2º import idêntico (idempotente, sem duplicar)
- atualização (preço/estoque/título/descrição) sem duplicar
- SKU novo (só variante nova), SKU removido (linha mantida, stock=0,
  is_active=False), imagem removida (galeria reflete, sem duplicatas)
- missing supplier price e moedas divergentes: rollback total, sem preço zero
- freight provider_unavailable (shipping_cost/landed_cost/freight_currency NULL,
  nunca zero)
- categoria com/sem mapeamento; draft fora do catálogo público
- produto já publicado não vira draft silenciosamente
- falha no meio -> rollback total
- tokens/segredos nunca persistidos
- VERCEL + ALIEXPRESS_DRAFT_IMPORT=1 -> import desativado
"""
import os
import tempfile
import time
import uuid

import pytest
from decimal import Decimal
from flask import Flask
from sqlalchemy import create_engine, text, inspect as sa_inspect

from loja import db
from loja.schema import ensure_schema
from loja.modules.models import (
    Category,
    Product,
    ProductImage,
    ProductSync,
    ProductVariant,
    Supplier,
    SupplierCategoryMapping,
    SupplierProduct,
)
from loja.integrations.aliexpress.importer import (
    DraftImportConflict,
    DraftImportError,
    CurrencyMismatchError,
    import_product_draft,
    SOURCE_ALIEXPRESS,
)
from loja.integrations.aliexpress.models import (
    AliExpressProductDetail,
    AliExpressProductSku,
)


def _sku(
    sku_id="1",
    sale_price="10.00",
    original_price=None,
    stock=5,
    attributes=None,
    currency="USD",
):
    return AliExpressProductSku(
        sku_id=str(sku_id),
        attributes=attributes or [],
        available_stock=stock,
        sale_price=Decimal(sale_price) if sale_price is not None else None,
        original_price=Decimal(original_price) if original_price is not None else None,
        currency=currency,
    )


def _detail(
    product_id="1005000000000001",
    title="Camiseta AliExpress",
    description="descricao",
    category_id="200003959",
    images=None,
    skus=None,
    currency="USD",
):
    return AliExpressProductDetail(
        product_id=product_id,
        title=title,
        description=description,
        category_id=category_id,
        images=images or [],
        skus=skus or [_sku()],
        currency=currency,
    )


def _fetch_product(external_id):
    return Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=str(external_id)
    ).first()


def test_duplicate_sku_in_payload_dedupes_single_variant(db):
    external_id = "1005000000000009d"
    detail = _detail(
        product_id=external_id,
        skus=[
            _sku(sku_id="1", sale_price="10.00", stock=5),
            _sku(sku_id="2", sale_price="15.00", stock=3),
            _sku(sku_id="1", sale_price="99.00", stock=9),  # sku_id duplicado
        ],
    )
    # A primeira ocorrência vence; a única row respeita a constraint
    # uq_product_variant_supplier (nunca explode em 2 rows).
    result = import_product_draft(detail=detail, seller_id="SELLER_DEDUP")

    assert result.created is True
    assert result.variants_created == 2
    product = _fetch_product(external_id)
    variants = ProductVariant.query.filter_by(
        supplier_product_id=result.supplier_product_id
    ).all()
    assert len(variants) == 2
    by_sku = {v.supplier_sku_id: v for v in variants}
    assert by_sku["1"].stock == 5
    assert by_sku["1"].price == Decimal("10.00")
    assert by_sku["2"].stock == 3

    # Reimport com o mesmo payload duplicado continua idempotente.
    again = import_product_draft(detail=detail, seller_id="SELLER_DEDUP")
    assert again.created is False
    assert again.variants_created == 0
    assert again.variants_updated == 2
    assert ProductVariant.query.filter_by(
        supplier_product_id=result.supplier_product_id
    ).count() == 2


# --------------------------------------------------------------------------
# 1º import e idempotência
# --------------------------------------------------------------------------

def test_first_import_creates_full_relation(db):
    external_id = "1005000000000001"
    detail = _detail(
        product_id=external_id,
        images=[
            "https://a/img.jpg",
            "",
            "https://a/img.jpg",  # duplicata
            "https://b/img.jpg",
        ],
        skus=[
            _sku(sku_id="1", sale_price="10.00", stock=5),
            _sku(sku_id="2", sale_price="15.00", stock=3),
        ],
    )
    result = import_product_draft(detail=detail, seller_id="SELLER_FIRST")

    assert result.created is True
    assert result.variants_created == 2
    assert result.variants_updated == 0
    assert result.variants_disabled == 0
    assert result.images == 2

    supplier = Supplier.query.filter_by(
        channel_type="aliexpress", seller_id="SELLER_FIRST"
    ).one()
    assert supplier.name == "AliExpress"
    assert supplier.default_currency == "USD"
    assert supplier.is_sync_enabled is False

    product = _fetch_product(external_id)
    assert product.name == "Camiseta AliExpress"
    assert product.description == "descricao"
    assert product.price == Decimal("10.00")
    assert product.cost == Decimal("10.00")
    assert product.is_draft is True
    assert product.is_active is False
    assert product.source == "aliexpress"
    assert product.external_id == external_id
    assert product.stock == 8

    supplier_product = SupplierProduct.query.filter_by(
        supplier_id=supplier.id, supplier_product_id=external_id
    ).one()
    assert supplier_product.product_id == product.id
    assert supplier_product.currency == "USD"
    assert supplier_product.cost == Decimal("10.00")
    assert supplier_product.external_sku_id is None

    variants = ProductVariant.query.filter_by(
        supplier_product_id=supplier_product.id
    ).all()
    assert len(variants) == 2
    by_sku = {v.supplier_sku_id: v for v in variants}
    assert by_sku["1"].price == Decimal("10.00")
    assert by_sku["1"].stock == 5
    assert by_sku["1"].sale_price is None
    assert by_sku["2"].price == Decimal("15.00")

    assert ProductImage.query.filter_by(product_id=product.id).count() == 2
    assert product.image1 == "https://a/img.jpg"
    assert product.image2 == "https://b/img.jpg"
    assert product.image3 is None

    syncs = ProductSync.query.filter_by(product_id=product.id).all()
    assert len(syncs) == 1
    assert syncs[0].supplier_id == supplier.id
    assert syncs[0].state == "draft"
    assert syncs[0].external_product_id == external_id

    assert result.product_id == product.id
    assert result.supplier_product_id == supplier_product.id


def test_second_import_is_idempotent(db):
    external_id = "1005000000000002"
    detail = _detail(
        product_id=external_id,
        images=["https://a/img.jpg", "https://b/img.jpg"],
        skus=[
            _sku(sku_id="1", sale_price="10.00", stock=5),
            _sku(sku_id="2", sale_price="15.00", stock=3),
        ],
    )
    first = import_product_draft(detail=detail, seller_id="SELLER_IDEMP")
    second = import_product_draft(detail=detail, seller_id="SELLER_IDEMP")

    assert second.created is False
    assert second.variants_created == 0
    assert second.variants_updated == 2
    assert second.variants_disabled == 0
    assert second.images == 2

    assert Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=external_id
    ).count() == 1
    assert Supplier.query.filter_by(
        channel_type="aliexpress", seller_id="SELLER_IDEMP"
    ).count() == 1
    assert ProductSync.query.filter_by(product_id=first.product_id).count() == 1


def test_reimport_updates_without_duplication(db):
    external_id = "1005000000000003"
    detail_v1 = _detail(
        product_id=external_id,
        title="Titulo v1",
        description="desc v1",
        skus=[_sku(sku_id="1", sale_price="10.00", stock=5)],
    )
    import_product_draft(detail=detail_v1, seller_id="SELLER_UPD")

    detail_v2 = _detail(
        product_id=external_id,
        title="Titulo v2",
        description="desc v2",
        skus=[_sku(sku_id="1", sale_price="12.50", stock=7)],
    )
    result = import_product_draft(detail=detail_v2, seller_id="SELLER_UPD")

    assert result.created is False
    assert Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=external_id
    ).count() == 1
    product = _fetch_product(external_id)
    assert product.name == "Titulo v2"
    assert product.description == "desc v2"
    assert product.price == Decimal("12.50")
    assert product.cost == Decimal("12.50")
    assert product.stock == 7
    supplier_product = SupplierProduct.query.filter_by(
        supplier_id=product.supplier_links[0].supplier_id,
        supplier_product_id=external_id,
    ).one()
    assert supplier_product.cost == Decimal("12.50")


# --------------------------------------------------------------------------
# variantes: novo / removido / atributos / estoque agregado
# --------------------------------------------------------------------------

def test_new_sku_adds_only_new_variant(db):
    external_id = "1005000000000004"
    first = import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[_sku(sku_id="1", stock=5), _sku(sku_id="2", stock=3)],
        ),
        seller_id="SELLER_VAR",
    )
    second = import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[
                _sku(sku_id="1", stock=5),
                _sku(sku_id="2", stock=3),
                _sku(sku_id="3", sale_price="20.00", stock=1),
            ],
        ),
        seller_id="SELLER_VAR",
    )
    assert second.variants_created == 1
    assert second.variants_updated == 2
    supplier_product = db.session.get(SupplierProduct, first.supplier_product_id)
    assert ProductVariant.query.filter_by(
        supplier_product_id=supplier_product.id
    ).count() == 3


def test_removed_sku_is_disabled_not_deleted(db):
    external_id = "1005000000000005"
    first = import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[_sku(sku_id="1", stock=5), _sku(sku_id="2", stock=3)],
        ),
        seller_id="SELLER_RM",
    )
    supplier_product = db.session.get(SupplierProduct, first.supplier_product_id)

    second = import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[_sku(sku_id="1", stock=5)],
        ),
        seller_id="SELLER_RM",
    )
    assert second.variants_disabled == 1
    variants = ProductVariant.query.filter_by(
        supplier_product_id=supplier_product.id
    ).all()
    assert len(variants) == 2
    by_sku = {v.supplier_sku_id: v for v in variants}
    assert by_sku["2"].stock == 0
    assert by_sku["2"].is_active is False
    assert by_sku["1"].is_active is True
    assert _fetch_product(external_id).stock == 5


def test_removed_image_updates_gallery_without_duplicates(db):
    external_id = "1005000000000006"
    import_product_draft(
        detail=_detail(
            product_id=external_id,
            images=["https://a/img.jpg", "https://b/img.jpg"],
        ),
        seller_id="SELLER_IMG",
    )
    result = import_product_draft(
        detail=_detail(
            product_id=external_id,
            images=["https://b/img.jpg", "https://b/img.jpg"],
        ),
        seller_id="SELLER_IMG",
    )
    assert result.images == 1
    product = _fetch_product(external_id)
    assert ProductImage.query.filter_by(product_id=product.id).count() == 1
    assert product.image1 == "https://b/img.jpg"
    assert product.image2 is None


def test_variant_attributes_and_color_size_helpers(db):
    external_id = "1005000000000007"
    attributes = [
        {"name": "Color", "value": "Preto"},
        {"name": "Tamanho", "value": "P"},
        {"name": "Material", "value": "Algodão"},
    ]
    import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[_sku(sku_id="1", attributes=attributes)],
        ),
        seller_id="SELLER_ATTR",
    )
    product = _fetch_product(external_id)
    variant = ProductVariant.query.filter_by(
        product_id=product.id, supplier_sku_id="1"
    ).one()
    assert variant.attributes == attributes
    assert variant.color == "Preto"
    assert variant.size == "P"


def test_sale_price_none_falls_back_to_original_price(db):
    external_id = "1005000000000008"
    import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[_sku(sku_id="1", sale_price=None, original_price="20.00", stock=2)],
        ),
        seller_id="SELLER_FB",
    )
    product = _fetch_product(external_id)
    assert product.price == Decimal("20.00")
    variant = ProductVariant.query.filter_by(
        product_id=product.id, supplier_sku_id="1"
    ).one()
    assert variant.price == Decimal("20.00")


def test_unknown_stock_treated_as_zero(db):
    external_id = "1005000000000009"
    import_product_draft(
        detail=_detail(
            product_id=external_id,
            skus=[_sku(sku_id="1", stock=None, sale_price="10.00")],
        ),
        seller_id="SELLER_STOCK",
    )
    product = _fetch_product(external_id)
    variant = ProductVariant.query.filter_by(
        product_id=product.id, supplier_sku_id="1"
    ).one()
    assert variant.stock == 0
    assert variant.is_active is False
    assert product.stock == 0


# --------------------------------------------------------------------------
# rollback: preço ausente / moedas divergentes / falha no meio
# --------------------------------------------------------------------------

def test_missing_supplier_price_rolls_back(db):
    external_id = "1005000000000010"
    detail = _detail(
        product_id=external_id,
        skus=[_sku(sku_id="1", sale_price=None, original_price=None, stock=5)],
    )
    with pytest.raises(DraftImportError) as excinfo:
        import_product_draft(detail=detail, seller_id="SELLER_NOPRICE")
    assert "missing_supplier_price" in str(excinfo.value)

    assert Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=external_id
    ).count() == 0
    assert Supplier.query.filter_by(
        channel_type="aliexpress", seller_id="SELLER_NOPRICE"
    ).count() == 0
    assert SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).count() == 0


def test_divergent_currencies_roll_back_without_fx(db):
    external_id = "1005000000000011"
    detail = _detail(
        product_id=external_id,
        skus=[
            _sku(sku_id="1", sale_price="10.00", currency="USD"),
            _sku(sku_id="2", sale_price="15.00", currency="BRL"),
        ],
    )
    with pytest.raises(CurrencyMismatchError) as excinfo:
        import_product_draft(detail=detail, seller_id="SELLER_CUR")
    assert "currency_mismatch" in str(excinfo.value)

    assert Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=external_id
    ).count() == 0
    assert SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).count() == 0


def test_mid_import_failure_rolls_back_everything(db, monkeypatch):
    external_id = "1005000000000012"
    import loja.integrations.aliexpress.importer as importer

    def _boom(*a, **k):
        raise RuntimeError("splat")

    monkeypatch.setattr(importer, "_apply_images", _boom)
    with pytest.raises(RuntimeError):
        import_product_draft(
            detail=_detail(product_id=external_id),
            seller_id="SELLER_MID",
        )

    assert Product.query.filter_by(
        source=SOURCE_ALIEXPRESS, external_id=external_id
    ).count() == 0
    assert Supplier.query.filter_by(
        channel_type="aliexpress", seller_id="SELLER_MID"
    ).count() == 0
    assert SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).count() == 0
    assert ProductImage.query.filter_by(
        product_id=None, url="https://a/img.jpg"
    ).count() == 0
    assert ProductSync.query.filter_by(
        external_product_id=external_id
    ).count() == 0


# --------------------------------------------------------------------------
# frete / categoria / catálogo público
# --------------------------------------------------------------------------

def test_freight_provider_unavailable_fields(db):
    external_id = "1005000000000013"
    result = import_product_draft(
        detail=_detail(product_id=external_id),
        seller_id="SELLER_FREIGHT",
    )
    supplier_product = db.session.get(SupplierProduct, result.supplier_product_id)
    assert supplier_product.freight_status == "provider_unavailable"
    assert supplier_product.shipping_cost is None
    assert supplier_product.landed_cost is None
    assert supplier_product.freight_currency is None


def test_freight_status_default_unknown(db):
    supplier = Supplier(name="Manual", channel_type="manual", default_currency="BRL")
    db.session.add(supplier)
    db.session.commit()
    sp = SupplierProduct(
        supplier_id=supplier.id,
        product_id=uuid.uuid4(),
        supplier_product_id="EXT_DEFAULT",
        cost=Decimal("5.00"),
        currency="BRL",
    )
    db.session.add(sp)
    db.session.commit()
    assert sp.freight_status == "unknown"


def test_category_with_mapping(db):
    external_id = "1005000000000014"
    supplier = Supplier(
        name="AliExpress",
        channel_type="aliexpress",
        seller_id="SELLER_CAT",
        default_currency="USD",
        is_sync_enabled=False,
    )
    category = Category(name="Roupas", slug="roupas-import")
    db.session.add(supplier)
    db.session.commit()
    db.session.add(category)
    db.session.flush()
    db.session.add(
        SupplierCategoryMapping(
            supplier_id=supplier.id,
            external_category_id="200003959",
            category_id=category.id,
            source="aliexpress",
        )
    )
    db.session.commit()

    result = import_product_draft(
        detail=_detail(product_id=external_id),
        seller_id="SELLER_CAT",
    )
    product = db.session.get(Product, result.product_id)
    assert product.category_id == category.id
    assert Supplier.query.filter_by(
        channel_type="aliexpress", seller_id="SELLER_CAT"
    ).count() == 1


def test_category_without_mapping_is_none(db):
    external_id = "1005000000000015"
    result = import_product_draft(
        detail=_detail(
            product_id=external_id, category_id="200003960"
        ),
        seller_id="SELLER_NOCAT",
    )
    product = db.session.get(Product, result.product_id)
    assert product.category is None
    assert product.category_id is None
    assert Category.query.filter_by(source="aliexpress").count() == 0


def test_draft_not_in_public_catalog(db):
    external_id = "1005000000000016"
    import_product_draft(
        detail=_detail(product_id=external_id),
        seller_id="SELLER_PUBLIC",
    )
    assert Product.query.filter_by(
        is_active=True, source=SOURCE_ALIEXPRESS
    ).filter(Product.external_id == external_id).count() == 0
    assert Product.query.filter_by(
        is_draft=False, source=SOURCE_ALIEXPRESS
    ).filter(Product.external_id == external_id).count() == 0


def test_published_aliexpress_product_is_conflict_not_silent_draft(db):
    external_id = "1005000000000017"
    published = Product(
        id=uuid.uuid4(),
        name="Ja publicado",
        description="decisao comercial",
        price=Decimal("99.90"),
        image1="https://x/p.jpg",
        stock=3,
        is_active=True,
        is_draft=False,
        source="aliexpress",
        external_id=external_id,
        pricing_status="priced",
    )
    db.session.add(published)
    db.session.commit()

    with pytest.raises(DraftImportConflict):
        import_product_draft(
            detail=_detail(product_id=external_id, title="Nao sobrescrever"),
            seller_id="SELLER_PUB",
        )

    product = _fetch_product(external_id)
    assert product.name == "Ja publicado"
    assert product.is_active is True
    assert product.is_draft is False
    assert SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).count() == 0


def test_published_with_link_conflict_not_silent_draft(db):
    external_id = "1005000000000018"
    import_product_draft(
        detail=_detail(product_id=external_id, title="Draft orcamental"),
        seller_id="SELLER_PUBLINK",
    )
    product = _fetch_product(external_id)
    assert product.is_draft is True

    product.is_draft = False
    product.is_active = True
    product.pricing_status = "priced"
    product.name = "Publicado depois"
    db.session.commit()

    with pytest.raises(DraftImportConflict):
        import_product_draft(
            detail=_detail(product_id=external_id, title="Nao sobrescrever 2"),
            seller_id="SELLER_PUBLINK",
        )

    product = _fetch_product(external_id)
    assert product.name == "Publicado depois"
    assert product.is_active is True
    assert product.is_draft is False


def test_existing_draft_product_without_link_is_reused(db):
    external_id = "1005000000000018b"
    draft = Product(
        id=uuid.uuid4(),
        name="Draft preexistente",
        price=Decimal("5.00"),
        is_active=False,
        is_draft=True,
        source="aliexpress",
        external_id=external_id,
    )
    db.session.add(draft)
    db.session.commit()

    result = import_product_draft(
        detail=_detail(product_id=external_id, title="Atualizado"),
        seller_id="SELLER_REUSE",
    )
    assert result.created is False
    assert SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).count() == 1
    assert _fetch_product(external_id).name == "Atualizado"


def test_duplicate_products_is_conflict(db):
    external_id = "1005000000000019"
    for i in range(2):
        db.session.add(
            Product(
                id=uuid.uuid4(),
                name=f"Duplicado {i}",
                price=Decimal("5.00"),
                is_active=False,
                is_draft=True,
                source="aliexpress",
                external_id=external_id,
            )
        )
    db.session.commit()
    with pytest.raises(DraftImportConflict):
        import_product_draft(
            detail=_detail(product_id=external_id),
            seller_id="SELLER_DUP",
        )


def test_tokens_and_secrets_never_persisted(db):
    external_id = "1005000000000020"
    import_product_draft(
        detail=_detail(product_id=external_id),
        seller_id="SELLER_TOK",
    )
    supplier = Supplier.query.filter_by(
        channel_type="aliexpress", seller_id="SELLER_TOK"
    ).one()
    assert supplier.api_key is None
    assert supplier.api_secret is None
    assert supplier.access_token is None
    assert supplier.access_token_expires_at is None


# --------------------------------------------------------------------------
# gating do callback (Vercel nunca escreve)
# --------------------------------------------------------------------------

def test_draft_import_enabled_vercel_blocks(monkeypatch):
    from loja.integrations.aliexpress.routes import _draft_import_enabled

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALIEXPRESS_DRAFT_IMPORT", "1")
    assert _draft_import_enabled() is False


def test_draft_import_enabled_opt_in_and_default(monkeypatch):
    from loja.integrations.aliexpress.routes import _draft_import_enabled

    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("ALIEXPRESS_DRAFT_IMPORT", "1")
    assert _draft_import_enabled() is True

    monkeypatch.setenv("ALIEXPRESS_DRAFT_IMPORT", "0")
    assert _draft_import_enabled() is False

    monkeypatch.delenv("ALIEXPRESS_DRAFT_IMPORT", raising=False)
    assert _draft_import_enabled() is False


# --------------------------------------------------------------------------
# callback integrado (draft import roda após product.get em local/dev)
# --------------------------------------------------------------------------

def _fake_token():
    from loja.integrations.aliexpress.oauth import AliExpressOAuthToken

    return AliExpressOAuthToken(
        access_token="REAL_AT",
        refresh_token="REAL_RT",
        seller_id="S_CB",
        expires_in=604800,
    )


class _CbClient:
    """Cliente falso: busca + detalhe + frete (para o smoke do callback)."""

    def __init__(self, product_id="1005000000000021", *a, **k):
        self._pid = product_id

    def search_products(self, **kw):
        from loja.integrations.aliexpress.models import AliExpressSearchResult
        from types import SimpleNamespace

        return AliExpressSearchResult(
            products=[SimpleNamespace(item_id=self._pid)],
            total=1,
            wrapper="aliexpress.ds.text.search_response",
        )

    def get_product(self, **kw):
        return _detail(
            product_id=self._pid,
            images=["https://cb/a.jpg"],
            skus=[_sku(sku_id="1", sale_price="9.90", currency="BRL")],
            currency="BRL",
        )

    def query_freight(self, **kw):
        from loja.integrations.aliexpress.models import (
            AliExpressFreightOption,
            AliExpressFreightResult,
        )

        return AliExpressFreightResult(
            options=[
                AliExpressFreightOption(
                    service_name="Cainiao",
                    shipping_fee=Decimal("12.50"),
                    currency="BRL",
                    delivery_days=15,
                    tracking=True,
                )
            ],
            currency="BRL",
            min_fee=Decimal("12.50"),
            min_delivery_days=15,
        )


def test_callback_runs_draft_import_when_enabled(app, db, monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        lambda *a, **k: _fake_token(),
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: _CbClient(product_id="1005000000000021"),
    )
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://www.pollomodas.com.br/cb")
    monkeypatch.setenv("ALIEXPRESS_DRAFT_IMPORT", "1")
    monkeypatch.delenv("VERCEL", raising=False)

    state = "ST_CB_DRAFT"
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["aliexpress_oauth_state"] = {"state": state, "ts": time.time() + 60}
    r = client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "Draft import: success" in body
    assert "variantes criadas/atualizadas/desativadas: 1/0/0" in body

    product = _fetch_product("1005000000000021")
    assert product is not None
    assert product.is_draft is True
    assert product.price == Decimal("9.90")
    assert "REAL_AT" not in body


def test_callback_skips_draft_import_when_disabled(app, db, monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        lambda *a, **k: _fake_token(),
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: _CbClient(product_id="1005000000000022"),
    )
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://www.pollomodas.com.br/cb")
    monkeypatch.delenv("ALIEXPRESS_DRAFT_IMPORT", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)

    state = "ST_CB_OFF"
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["aliexpress_oauth_state"] = {"state": state, "ts": time.time() + 60}
    r = client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "Draft import" not in body
    assert _fetch_product("1005000000000022") is None


# --------------------------------------------------------------------------
# ensure_schema aditivo (colunas novas em tabelas antigas)
# --------------------------------------------------------------------------

def test_ensure_schema_adds_new_columns_to_old_tables():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    uri = f"sqlite:///{tmp.name}"

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SECRET_KEY"] = "test-secret-key"
    app.config["TESTING"] = True
    db.init_app(app)

    pre = create_engine(uri)
    with pre.connect() as c:
        c.execute(text(
            "CREATE TABLE supplier_products "
            "(id VARCHAR(36) PRIMARY KEY, supplier_product_id VARCHAR(64))"
        ))
        c.execute(text(
            "CREATE TABLE product_variants "
            "(id VARCHAR(36) PRIMARY KEY, supplier_sku_id VARCHAR(64))"
        ))
        c.execute(text(
            "CREATE TABLE products "
            "(id VARCHAR(36) PRIMARY KEY, name TEXT NOT NULL, price NUMERIC(10,2) "
            "NOT NULL, is_active BOOLEAN NOT NULL DEFAULT 1, source VARCHAR(20) "
            "NOT NULL DEFAULT 'internal', is_draft BOOLEAN NOT NULL DEFAULT 0)"
        ))
        c.commit()

    try:
        with app.app_context():
            db.create_all()
            ensure_schema(app)
            inspector = sa_inspect(db.engine)
            sp_cols = {c["name"] for c in inspector.get_columns("supplier_products")}
            pv_cols = {c["name"] for c in inspector.get_columns("product_variants")}
            p_cols = {c["name"] for c in inspector.get_columns("products")}
            assert {
                "freight_status", "shipping_cost", "landed_cost", "freight_currency",
            }.issubset(sp_cols)
            assert "attributes" in pv_cols
            assert {
                "currency", "target_margin_percent", "pricing_status",
                "pricing_error", "pricing_updated_at",
            }.issubset(p_cols)
            # idempotente: segunda execução não duplica
            ensure_schema(app)
            sp_cols2 = {c["name"] for c in sa_inspect(db.engine).get_columns("supplier_products")}
            assert sp_cols == sp_cols2
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def test_new_columns_created_by_create_all(db):
    inspector = sa_inspect(db.engine)
    sp_cols = {c["name"] for c in inspector.get_columns("supplier_products")}
    pv_cols = {c["name"] for c in inspector.get_columns("product_variants")}
    p_cols = {c["name"] for c in inspector.get_columns("products")}
    assert {"freight_status", "shipping_cost", "landed_cost", "freight_currency"}.issubset(sp_cols)
    assert "attributes" in pv_cols
    assert {
        "currency", "target_margin_percent", "pricing_status",
        "pricing_error", "pricing_updated_at",
    }.issubset(p_cols)


# --------------------------------------------------------------------------
# Fase 6: estado de pricing no import de rascunho
# --------------------------------------------------------------------------

def test_new_draft_starts_blocked_with_currency(db):
    external_id = "1005000000000042"
    detail = _detail(
        product_id=external_id,
        currency="USD",
        skus=[_sku(sku_id="1", sale_price="10.00", stock=5, currency="USD")],
    )
    result = import_product_draft(detail=detail, seller_id="SELLER_F6")
    assert result.created is True

    product = _fetch_product(external_id)
    assert product.currency == "USD"
    assert product.pricing_status == "blocked"
    assert product.pricing_error == "freight_unavailable"
    assert product.target_margin_percent is None
    assert product.is_draft is True
    assert product.is_active is False

    supplier_product = SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).one()
    assert supplier_product.freight_status == "provider_unavailable"
    assert supplier_product.shipping_cost is None
    assert supplier_product.landed_cost is None


def test_reimport_preserves_priced_decisions(db):
    external_id = "1005000000000043"
    detail = _detail(
        product_id=external_id,
        currency="USD",
        skus=[_sku(sku_id="1", sale_price="10.00", stock=5, currency="USD")],
    )
    import_product_draft(detail=detail, seller_id="SELLER_F6P")
    product = _fetch_product(external_id)

    # Cenário comercial futuro: frete confirmado + precificado (ainda draft).
    sp = supplier_product = SupplierProduct.query.filter_by(
        supplier_product_id=external_id
    ).one()
    sp.freight_status = "available"
    sp.shipping_cost = Decimal("2.00")
    sp.freight_currency = "USD"
    sp.landed_cost = Decimal("12.00")
    product.price = Decimal("15.00")
    product.pricing_status = "priced"
    product.target_margin_percent = Decimal("20.00")
    product.pricing_updated_at = None
    db.session.commit()

    reimport = import_product_draft(detail=detail, seller_id="SELLER_F6P")
    assert reimport.created is False

    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "priced"
    assert product.price == Decimal("15.00")
    assert sp.freight_status == "available"
    assert sp.shipping_cost == Decimal("2.00")
    assert sp.landed_cost == Decimal("12.00")


# --------------------------------------------------------------------------
# Fase 6: reimport com mudança material invalida pricing (stale)
# --------------------------------------------------------------------------

def _priced_draft(db, external_id, sku_price="10.00", currency="USD"):
    from loja.services.pricing import price_dropshipping_product

    detail = _detail(
        product_id=external_id,
        currency=currency,
        skus=[_sku(sku_id="1", sale_price=sku_price, stock=5, currency=currency)],
    )
    import_product_draft(detail=detail, seller_id="SELLER_STALE")
    product = _fetch_product(external_id)
    sp = SupplierProduct.query.filter_by(supplier_product_id=external_id).one()
    sp.freight_status = "available"
    sp.shipping_cost = Decimal("2.00")
    sp.freight_currency = currency
    db.session.flush()
    price_dropshipping_product(product=product, target_margin_percent="20")
    db.session.commit()
    db.session.refresh(product)
    db.session.refresh(sp)
    return product, sp, detail


def _assert_publication_blocked(product):
    from loja.services.publication import evaluate_publication

    decision = evaluate_publication(product)
    assert decision.allowed is False
    assert "pricing_not_ready" in decision.reasons


def test_priced_and_cost_increase_goes_stale(db):
    product, sp, _ = _priced_draft(db, "1005000000000052")
    assert product.pricing_status == "priced"
    assert product.price == Decimal("15.00")  # landed (10+2)=12 / 0.8
    assert sp.landed_cost == Decimal("12.00")

    reimport = import_product_draft(
        detail=_detail(
            product_id="1005000000000052",
            skus=[_sku(sku_id="1", sale_price="20.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    assert reimport.created is False

    db.session.refresh(product)
    db.session.refresh(sp)
    assert sp.cost == Decimal("20.00")
    assert sp.freight_status == "available"
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"
    assert sp.landed_cost == Decimal("22.00")  # custo novo + frete recalculado
    assert product.price == Decimal("15.00")   # preço de venda NÃO muda
    assert product.target_margin_percent == Decimal("20.00")
    _assert_publication_blocked(product)


def test_priced_and_cost_decrease_goes_stale(db):
    product, sp, _ = _priced_draft(db, "1005000000000053")

    import_product_draft(
        detail=_detail(
            product_id="1005000000000053",
            skus=[_sku(sku_id="1", sale_price="5.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )

    db.session.refresh(product)
    db.session.refresh(sp)
    assert sp.cost == Decimal("5.00")
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"
    assert sp.landed_cost == Decimal("7.00")  # 5 + 2
    assert product.price == Decimal("15.00")
    _assert_publication_blocked(product)


def test_currency_change_goes_stale_with_null_landed(db):
    product, sp, _ = _priced_draft(db, "1005000000000054")
    assert sp.landed_cost == Decimal("12.00")

    reimport = import_product_draft(
        detail=_detail(
            product_id="1005000000000054",
            currency="EUR",
            skus=[_sku(sku_id="1", sale_price="10.00", stock=5, currency="EUR")],
        ),
        seller_id="SELLER_STALE",
    )
    assert reimport.created is False

    db.session.refresh(product)
    db.session.refresh(sp)
    assert sp.currency == "EUR"
    assert product.currency == "EUR"
    assert product.pricing_status == "stale"
    assert product.pricing_error == "currency_changed"
    assert sp.landed_cost is None
    assert product.price == Decimal("15.00")  # preservado para auditoria
    _assert_publication_blocked(product)


def test_same_cost_reimport_keeps_priced_after_freight(db):
    product, sp, detail = _priced_draft(db, "1005000000000055")

    import_product_draft(detail=detail, seller_id="SELLER_STALE")

    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "priced"
    assert product.price == Decimal("15.00")
    assert sp.cost == Decimal("10.00")
    assert sp.landed_cost == Decimal("12.00")


def _priced_draft_with(
    db, external_id, sku_price, shipping, margin="20", currency="USD"
):
    from loja.services.pricing import price_dropshipping_product

    detail = _detail(
        product_id=external_id,
        currency=currency,
        skus=[_sku(sku_id="1", sale_price=str(sku_price), stock=5, currency=currency)],
    )
    import_product_draft(detail=detail, seller_id="SELLER_STALE")
    product = _fetch_product(external_id)
    sp = SupplierProduct.query.filter_by(supplier_product_id=external_id).one()
    sp.freight_status = "available"
    sp.shipping_cost = Decimal(str(shipping))
    sp.freight_currency = currency
    db.session.flush()
    price_dropshipping_product(product=product, target_margin_percent=margin)
    db.session.commit()
    db.session.refresh(product)
    db.session.refresh(sp)
    return product, sp, detail


def test_stale_reconciles_landed_across_consecutive_reimports(db):
    # priced: cost 80 + shipping 20 -> landed 100; sale = 100 / 0.8 = 125
    product, sp, _ = _priced_draft_with(db, "1005000000000060", 80, 20)
    assert product.pricing_status == "priced"
    assert product.price == Decimal("125.00")
    assert sp.landed_cost == Decimal("100.00")

    import_product_draft(
        detail=_detail(
            product_id="1005000000000060",
            skus=[_sku(sku_id="1", sale_price="90.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"
    assert sp.landed_cost == Decimal("110.00")  # 90 + 20

    import_product_draft(
        detail=_detail(
            product_id="1005000000000060",
            skus=[_sku(sku_id="1", sale_price="100.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "stale"               # continua stale
    assert product.pricing_error == "supplier_cost_changed"
    assert sp.landed_cost == Decimal("120.00")             # 100 + 20 (NOVO custo)
    assert product.price == Decimal("125.00")              # preço preservado
    assert product.target_margin_percent == Decimal("20.00")  # margem preservada
    _assert_publication_blocked(product)


def test_stale_reimport_without_changes_preserves_state(db):
    product, sp, _ = _priced_draft_with(db, "1005000000000061", 80, 20)

    import_product_draft(
        detail=_detail(
            product_id="1005000000000061",
            skus=[_sku(sku_id="1", sale_price="90.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "stale"
    assert sp.landed_cost == Decimal("110.00")
    sale_price = product.price
    margin = product.target_margin_percent

    import_product_draft(
        detail=_detail(
            product_id="1005000000000061",
            skus=[_sku(sku_id="1", sale_price="90.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "stale"               # inalterado
    assert product.pricing_error == "supplier_cost_changed"
    assert sp.landed_cost == Decimal("110.00")             # inalterado
    assert product.currency == "USD"
    assert product.price == sale_price
    assert product.target_margin_percent == margin


def test_stale_keeps_stale_after_further_currency_change(db):
    product, sp, _ = _priced_draft_with(db, "1005000000000062", 80, 20)

    import_product_draft(
        detail=_detail(
            product_id="1005000000000062",
            skus=[_sku(sku_id="1", sale_price="90.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    assert product.pricing_status == "stale"

    import_product_draft(
        detail=_detail(
            product_id="1005000000000062",
            currency="EUR",
            skus=[_sku(sku_id="1", sale_price="100.00", stock=5, currency="EUR")],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "stale"
    assert product.pricing_error == "currency_changed"
    assert sp.landed_cost is None
    assert product.price == Decimal("125.00")              # preservado
    _assert_publication_blocked(product)


def test_stale_when_freight_becomes_unusable_on_reimport(db):
    product, sp, _ = _priced_draft_with(db, "1005000000000063", 80, 20)
    assert product.pricing_status == "priced"
    assert sp.landed_cost == Decimal("100.00")

    sp.freight_status = "provider_unavailable"
    sp.shipping_cost = None
    sp.freight_currency = None
    db.session.commit()

    import_product_draft(
        detail=_detail(
            product_id="1005000000000063",
            skus=[_sku(sku_id="1", sale_price="90.00", stock=5)],
        ),
        seller_id="SELLER_STALE",
    )
    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"
    assert sp.landed_cost is None                         # frete inutilizável
    assert product.price == Decimal("125.00")             # preservado
    _assert_publication_blocked(product)