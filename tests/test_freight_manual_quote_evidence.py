"""PASSO 2–4: frete manual com evidência — rota manual fim a fim (sem rede).

Cobre a decisão da sessão de frete (provider AliExpress indisponível →
rota manual via ``supplier_reference_freight_quotes``):

- checkout agrupado cobra a cotação vigente do banco (não o R$ 10);
- sem cotação vigente o checkout bloqueia (nunca presume valor);
- cotação vencida exige recotação;
- helper admin persiste a evidência com validade;
- margem real vs. assumida (risco oculto do flat).
"""
from datetime import timedelta
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import text

from loja.services.shipping_quote import (
    QUOTE_TTL_SECONDS,
    QuoteUnavailable,
    quote_for_items,
)
from loja.utils import utcnow

REF_DEST_CEP = "77000000"
REF_PREFIX = "770"


@pytest.fixture(autouse=True)
def reference_quote_table(db):
    db.session.execute(
        text(
            """CREATE TABLE IF NOT EXISTS supplier_reference_freight_quotes (
                supplier_product_id VARCHAR(36) NOT NULL,
                dest_prefix VARCHAR(3) NOT NULL,
                option_key VARCHAR(120) NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL,
                currency VARCHAR(3) NOT NULL,
                cost NUMERIC(12,2) NOT NULL,
                min_days INTEGER,
                max_days INTEGER,
                quoted_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                provider_method VARCHAR(120) NOT NULL,
                evidence_ref_hash VARCHAR(64),
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (supplier_product_id, dest_prefix, option_key, quantity),
                CHECK (length(dest_prefix) = 3),
                CHECK (quantity > 0),
                CHECK (currency = 'BRL'),
                CHECK (cost >= 0),
                CHECK (expires_at > quoted_at)
            )"""
        )
    )
    db.session.execute(text("DELETE FROM supplier_reference_freight_quotes"))
    db.session.commit()
    yield


def _supplier_link(db, *, cost="60.00"):
    from loja.modules.models import (
        Product,
        ProductVariant,
        Supplier,
        SupplierProduct,
    )

    supplier = Supplier(
        id=uuid.uuid4(), name="Supp Manual", channel_type="aliexpress",
        seller_id="SELLER_MANUAL",
    )
    db.session.add(supplier)
    db.session.flush()
    product = Product(
        id=uuid.uuid4(), name="Prod Manual", price=Decimal("129.90"),
        currency="BRL", source="aliexpress", external_id="EXT_MANUAL",
        is_draft=True, is_active=False,
    )
    db.session.add(product)
    db.session.flush()
    sp = SupplierProduct(
        id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id,
        supplier_product_id="SP_MANUAL",
        cost=Decimal(cost), currency="BRL",
        freight_status="available",
        shipping_cost=Decimal("18.50"), freight_currency="BRL",
        is_active=True,
    )
    db.session.add(sp)
    db.session.flush()
    variant = ProductVariant(
        id=uuid.uuid4(), product_id=product.id,
        supplier_product_id=sp.id, supplier_sku_id="SKU_MANUAL",
        stock=5, is_active=True,
    )
    db.session.add(variant)
    db.session.commit()
    return product, variant, sp


def _enable_grouped(client, monkeypatch):
    monkeypatch.setitem(
        client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True
    )
    monkeypatch.setitem(
        client.application.config, "SALES_READINESS_DESTINATION_CEP", REF_DEST_CEP
    )
    # Opção ausente de propósito: grupos de checkout não têm opção.
    monkeypatch.delitem(
        client.application.config, "SALES_READINESS_SHIPPING_OPTION", raising=False
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_ALIEXPRESS_FRESH_QUOTES", {}
    )


def _store_manual_quote(sp, *, cost="18.50", dest_cep=REF_DEST_CEP,
                        option=None, qty=1, ttl_seconds=30 * 86400,
                        min_days=8, max_days=14, now=None,
                        provider_method="admin_manual",
                        evidence_ref="COT-TEST-001"):
    from loja.services.supplier_freight_quotes import store_reference_freight_quote

    now = now or utcnow()
    return store_reference_freight_quote(
        supplier_product_id=sp.id,
        dest_cep=dest_cep,
        option=option,
        qty=qty,
        quote={
            "dest_prefix": dest_cep[:3],
            "option": option,
            "qty": qty,
            "currency": "BRL",
            "cost": cost,
            "min_days": min_days,
            "max_days": max_days,
            "quoted_at": now,
            "ttl_seconds": ttl_seconds,
        },
        provider_method=provider_method,
        evidence_ref=evidence_ref,
        now=now,
    )


# --- PASSO 3.2: checkout agrupado cobra a cotação real, não o flat --------

def test_grouped_checkout_charges_db_quote_not_flat(db, client, monkeypatch):
    product, variant, sp = _supplier_link(db)
    _enable_grouped(client, monkeypatch)
    _store_manual_quote(sp, cost="18.50")

    result = quote_for_items(
        [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
        REF_DEST_CEP,
    )
    assert result["total"] == Decimal("18.50")
    assert result["total"] != Decimal("10.00")


def test_default_providers_merges_db_snapshot(db, client, monkeypatch):
    from loja.services.shipping_quote import default_providers

    _, _, sp = _supplier_link(db)
    _enable_grouped(client, monkeypatch)
    _store_manual_quote(sp, cost="18.50")

    providers = default_providers()
    fresh = providers["aliexpress"].fresh_quotes
    assert str(sp.id) in fresh
    assert Decimal(str(fresh[str(sp.id)]["cost"])) == Decimal("18.50")


# --- PASSO 2.2 / 3.4: sem cotação vigente, a venda bloqueia ---------------

def test_grouped_without_quote_blocks_sale(db, client, monkeypatch):
    product, variant, _ = _supplier_link(db)
    _enable_grouped(client, monkeypatch)

    with pytest.raises(QuoteUnavailable) as exc:
        quote_for_items(
            [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
            REF_DEST_CEP,
        )
    assert exc.value.code == "unverified_cost"

    from loja.views.payments_routes import quote_checkout_shipping

    with pytest.raises(ValueError, match="Frete indisponível"):
        quote_checkout_shipping(
            [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
            REF_DEST_CEP,
        )


def test_expired_quote_blocks_until_requoted(db, client, monkeypatch):
    from loja.services.supplier_freight_quotes import find_valid_reference_quotes

    product, variant, sp = _supplier_link(db)
    _enable_grouped(client, monkeypatch)
    now = utcnow()
    _store_manual_quote(sp, cost="18.50", ttl_seconds=3600, now=now)

    future = now + timedelta(seconds=7200)
    assert find_valid_reference_quotes(
        dest_cep=REF_DEST_CEP, option=None, qty=1, now=future
    ) == {}

    with pytest.raises(QuoteUnavailable):
        quote_for_items(
            [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
            REF_DEST_CEP,
            now=future,
        )


def test_flat_temporario_preservado_quando_grouped_desligado(
    db, client, monkeypatch
):
    from loja.views.payments_routes import quote_checkout_shipping

    product, variant, _ = _supplier_link(db)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_TEMPORARY_FLAT_RATE", "10.00"
    )
    shipping = quote_checkout_shipping(
        [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
        REF_DEST_CEP,
    )
    assert shipping == Decimal("10.00")


# --- PASSO 2.4: helper admin persiste a evidência com validade -------------

def test_admin_persist_helper_roundtrip(db, client):
    from flask import current_app

    from loja.services.supplier_freight_quotes import find_valid_reference_quotes
    from loja.views.aliexpress_admin_routes import _persist_reference_freight_quote

    _, _, sp = _supplier_link(db)
    form = {
        "freight_dest_cep": REF_DEST_CEP,
        "freight_ttl_days": "30",
        "freight_evidence_ref": "COT-ADMIN-001",
        "freight_min_days": "8",
        "freight_max_days": "14",
    }
    with current_app.test_request_context("/", method="POST", data=form):
        note = _persist_reference_freight_quote(sp, Decimal("18.50"), "BRL")

    assert "vigente até" in note
    snapshot = find_valid_reference_quotes(
        dest_cep=REF_DEST_CEP, option=None, qty=1
    )
    assert str(sp.id) in snapshot
    assert snapshot[str(sp.id)]["cost"] == Decimal("18.50")


def test_admin_persist_helper_sem_cenario_nao_quebra(db, client, monkeypatch):
    from flask import current_app

    from loja.views.aliexpress_admin_routes import _persist_reference_freight_quote

    _, _, sp = _supplier_link(db)
    monkeypatch.delitem(
        current_app.config, "SALES_READINESS_DESTINATION_CEP", raising=False
    )
    with current_app.test_request_context("/", method="POST", data={}):
        note = _persist_reference_freight_quote(sp, Decimal("18.50"), "BRL")
    assert "não persistida" in note


# --- PASSO 3.3: margem real vs. assumida (risco oculto do flat) ------------

def test_margin_real_vs_assumida(db):
    from loja.services.pricing import calculate_landed_cost

    _, _, sp = _supplier_link(db, cost="60.00")
    sp.shipping_cost = Decimal("10.00")
    landed_assumido = calculate_landed_cost(sp)
    sp.shipping_cost = Decimal("18.50")
    landed_real = calculate_landed_cost(sp)

    assert landed_assumido == Decimal("70.00")
    assert landed_real == Decimal("78.50")
    # Risco oculto: R$ 8,50 de custo logístico fora da conta por pedido.
    assert landed_real - landed_assumido == Decimal("8.50")

    from loja.services.pricing import (
        price_dropshipping_product,
        sale_price_for_margin,
    )
    from loja.modules.models import Product

    product = Product.query.filter_by(id=sp.product_id).one()
    sp.shipping_cost = Decimal("10.00")
    price_flat = price_dropshipping_product(
        product=product, target_margin_percent=Decimal("30")
    ).sale_price
    sp.shipping_cost = Decimal("18.50")
    price_real = price_dropshipping_product(
        product=product, target_margin_percent=Decimal("30")
    ).sale_price
    # Mesma margem-alvo exige preço maior; vender pelo preço do flat
    # comprimiria a margem real. O delta equivale à diferença de landed
    # diluída na margem (arredondamento para cima nunca reduz a margem).
    assert price_flat == sale_price_for_margin(Decimal("70.00"), Decimal("30"))
    assert price_real == sale_price_for_margin(Decimal("78.50"), Decimal("30"))
    assert price_real - price_flat >= Decimal("12.14")
