"""Guarda de publicação (Fase 6): evaluate_publication + publish_product +
rota de ativação do admin. Códigos estáveis, nunca mensagens livres.
"""
import uuid
from decimal import Decimal

import pytest

from loja.integrations.aliexpress.importer import SOURCE_ALIEXPRESS
from loja.modules.models import Product, ProductVariant, SupplierProduct
from loja.services import pricing
from loja.services.publication import (
    PublicationBlocked,
    evaluate_publication,
    publish_product,
)


def _prepared(
    db,
    *,
    margin="20",
    price="150.00",
    landed="110.00",
    freight_status="available",
    shipping_cost="10.00",
    cost="100.00",
    currency="BRL",
    freight_currency="BRL",
    pricing_status=pricing.PRICED,
    source=SOURCE_ALIEXPRESS,
    variant_stock=5,
    product_stock=5,
    is_active=False,
    is_draft=True,
):
    product = Product(
        id=uuid.uuid4(),
        name="Blusinha PG",
        description="teste",
        price=Decimal(price),
        stock=product_stock,
        is_active=is_active,
        is_draft=is_draft,
        source=source,
        external_id="1005" + uuid.uuid4().hex[:12],
        currency=currency,
        pricing_status=pricing_status,
        target_margin_percent=Decimal(margin) if margin is not None else None,
    )
    db.session.add(product)
    db.session.flush()
    supplier_product = SupplierProduct(
        id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id="SP_" + uuid.uuid4().hex[:8],
        cost=Decimal(cost),
        currency=currency,
        freight_status=freight_status,
        shipping_cost=Decimal(shipping_cost),
        landed_cost=Decimal(landed) if landed is not None else None,
        freight_currency=freight_currency,
        is_active=True,
    )
    db.session.add(supplier_product)
    db.session.flush()
    variant = ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        stock=variant_stock,
        is_active=True,
    )
    db.session.add(variant)
    db.session.flush()
    return product, supplier_product


def _assert_not_published(product, reasons):
    decision = evaluate_publication(product)
    assert decision.allowed is False
    assert set(reasons).issubset(decision.reasons)
    before = (product.is_draft, product.is_active)
    with pytest.raises(PublicationBlocked) as excinfo:
        publish_product(product)
    assert excinfo.value.args[0] == "publication_blocked"
    assert set(reasons).issubset(excinfo.value.reasons)
    assert (product.is_draft, product.is_active) == before


# --------------------------------------------------------------------------
# draft atual nunca é publicado silenciosamente
# --------------------------------------------------------------------------

def test_unpriced_draft_never_publishes(db):
    product, _ = _prepared(db, pricing_status=pricing.UNPRICED, landed=None)
    _assert_not_published(product, ["landed_cost_missing", "pricing_not_ready"])


# --------------------------------------------------------------------------
# priced sem landed cost total conhecido nunca publica
# --------------------------------------------------------------------------

def test_priced_without_landed_never_publishes(db):
    product, _ = _prepared(db, landed=None)
    _assert_not_published(product, ["landed_cost_missing"])


def test_priced_with_landed_zero_never_publishes(db):
    product, _ = _prepared(db, landed="0")
    _assert_not_published(product, ["landed_cost_missing"])


# --------------------------------------------------------------------------
# sem margem alvo nunca publica
# --------------------------------------------------------------------------

def test_price_without_margin_never_publishes(db):
    product, _ = _prepared(db, margin=None)
    _assert_not_published(product, ["margin_missing"])


# --------------------------------------------------------------------------
# sem estoque vendável nunca publica
# --------------------------------------------------------------------------

def test_no_sellable_stock_never_publishes(db):
    product, _ = _prepared(db, variant_stock=0, product_stock=0)
    _assert_not_published(product, ["no_sellable_variant"])


def test_no_variant_active_never_publishes(db):
    product, supplier_product = _prepared(db)
    supplier_product.is_active = True
    product.variants[0].is_active = False
    db.session.flush()
    _assert_not_published(product, ["no_sellable_variant"])


# --------------------------------------------------------------------------
# preço abaixo da margem alvo nunca publica
# --------------------------------------------------------------------------

def test_price_below_target_margin_never_publishes(db):
    # landed 110 => preço mínimo para 20% = 137.50; 100 < 137.50 => bloqueia
    product, _ = _prepared(db, price="100.00")
    _assert_not_published(product, ["margin_below_target"])


# --------------------------------------------------------------------------
# frete indisponível / custo ausente / moeda divergente
# --------------------------------------------------------------------------

def test_freight_unavailable_blocks_publication(db):
    product, _ = _prepared(db, freight_status="provider_unavailable", landed=None)
    _assert_not_published(product, ["freight_unavailable"])


def test_landed_stale_blocks_publication(db):
    # cost(100) + shipping(10) = 110; landed guardado como 111 -> inconsistência
    # de banco/edição manual jamais publica com landed desatualizado.
    product, _ = _prepared(db, landed="111.00")
    _assert_not_published(product, ["landed_cost_stale"])


def test_negative_shipping_blocks_publication(db):
    # objetos em memória (nunca persistidos): a avaliação de publicação rejeita
    # shipping < 0 no serviço, sem depender do CHECK de banco.
    product = Product(
        id=uuid.uuid4(), name="NegPG", description="t", price=Decimal("150.00"),
        stock=5, is_active=False, is_draft=True, source=SOURCE_ALIEXPRESS,
        external_id="1005" + uuid.uuid4().hex[:12], currency="BRL",
        pricing_status=pricing.PRICED, target_margin_percent=Decimal("20"),
    )
    supplier_product = SupplierProduct(
        id=uuid.uuid4(), supplier_id=uuid.uuid4(),
        product_id=product.id, supplier_product_id="SP_NEGPUB",
        cost=Decimal("100.00"), currency="BRL",
        freight_status="available", shipping_cost=Decimal("-0.01"),
        landed_cost=Decimal("110.00"), freight_currency="BRL",
        is_active=True,
    )
    supplier_product.product = product
    variant = ProductVariant(
        id=uuid.uuid4(), product_id=product.id,
        supplier_product_id=supplier_product.id, stock=5, is_active=True,
    )
    variant.product = product
    _assert_not_published(product, ["shipping_cost_invalid"])


def test_variant_only_from_inactive_supplier_gives_no_sellable(db):
    product, sp = _prepared(db, variant_stock=0)
    product.variants[0].is_active = False
    inactive_supplier = SupplierProduct(
        id=uuid.uuid4(), supplier_id=sp.supplier_id, product_id=product.id,
        supplier_product_id="SP_INACTIVE", cost=Decimal("100.00"),
        currency="BRL", freight_status="available",
        shipping_cost=Decimal("10.00"), landed_cost=Decimal("110.00"),
        freight_currency="BRL", is_active=False,
    )
    db.session.add(inactive_supplier)
    db.session.flush()
    # A variante ativa pertence ao vínculo de fornecedor INATIVO: não conta.
    db.session.add(ProductVariant(
        id=uuid.uuid4(), product_id=product.id,
        supplier_product_id=inactive_supplier.id, stock=5, is_active=True,
    ))
    db.session.flush()
    _assert_not_published(product, ["no_sellable_variant"])


def test_currency_mismatch_blocks_publication(db):
    product, _ = _prepared(db, freight_currency="USD", landed=None)
    _assert_not_published(product, ["currency_mismatch"])


# --------------------------------------------------------------------------
# produto totalmente pronto publica
# --------------------------------------------------------------------------

def test_ready_product_publishes(db):
    product, supplier_product = _prepared(db)
    publish_product(product)
    db.session.commit()
    db.session.refresh(product)
    assert product.is_draft is False
    assert product.is_active is True
    assert evaluate_publication(product).allowed is True


# --------------------------------------------------------------------------
# produto interno/legado: fluxo legado não muda
# --------------------------------------------------------------------------

def test_internal_product_uses_legacy_flow(db):
    product, _ = _prepared(
        db, source="internal", pricing_status=pricing.UNPRICED,
        margin=None, landed=None,
    )
    assert evaluate_publication(product).allowed is True
    publish_product(product)
    db.session.commit()
    db.session.refresh(product)
    assert product.is_draft is False
    assert product.is_active is True


# --------------------------------------------------------------------------
# integração: ativação pelo admin
# --------------------------------------------------------------------------

def _post_edit(client, product_id, *, is_active):
    return client.post(
        f"/admin/products/edit/{product_id}",
        data={
            "name": "Blusinha PG",
            "description": "teste",
            "price": "150.00",
            "image1": "https://exemplo.com/img.png",
            "category": "feminino",
            "stock": "5",
            "is_active": "on" if is_active else "off",
        },
    )


def test_admin_cannot_activate_blocked_aliexpress(app, db, client, admin_user, login):
    login(admin_user.email)
    product, _ = _prepared(db, pricing_status=pricing.UNPRICED, landed=None)
    db.session.commit()

    r = _post_edit(client, product.id, is_active=True)
    assert r.status_code == 200
    body = r.data.decode()
    assert "Não foi possível publicar o produto AliExpress" in body

    db.session.refresh(product)
    assert product.is_draft is True
    assert product.is_active is False


def test_admin_can_activate_ready_aliexpress(app, db, client, admin_user, login):
    login(admin_user.email)
    product, _ = _prepared(db)
    db.session.commit()

    r = _post_edit(client, product.id, is_active=True)
    assert r.status_code == 302

    db.session.refresh(product)
    assert product.is_draft is False
    assert product.is_active is True


def test_admin_can_inactivate_without_publish_guard(app, db, client, admin_user, login):
    login(admin_user.email)
    product, _ = _prepared(db)
    db.session.commit()

    r = _post_edit(client, product.id, is_active=False)
    assert r.status_code == 302

    db.session.refresh(product)
    assert product.is_active is False
    assert product.is_draft is True


def test_stable_reason_codes_no_free_text(db):
    product, _ = _prepared(
        db, pricing_status=pricing.UNPRICED, landed=None,
        freight_status="provider_unavailable",
    )
    reasons = evaluate_publication(product).reasons
    for code in reasons:
        assert code not in {
            name for name in ("UNPRICED", "PROVIDER_UNAVAILABLE", "None")
        }
        assert " " not in code or code in {"no_sellable_variant"}