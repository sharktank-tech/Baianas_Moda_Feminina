"""FASE 9.0.0-F — feed Google Merchant gated e sem publicação externa."""

import uuid
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from loja.modules.models import Product
from loja.services.google_merchant_feed import (
    build_google_merchant_feed,
    find_google_merchant_products,
    is_google_merchant_feed_enabled,
)


GOOGLE_NS = {"g": "http://base.google.com/ns/1.0"}


def _product(**overrides):
    values = dict(
        id=uuid.uuid4(),
        name="Vestido Floral",
        description="<b>Vestido leve</b> para dias quentes.",
        price=Decimal("89.90"),
        currency="BRL",
        stock=3,
        image1="https://cdn.example/vestido.jpg",
        category="feminino",
        is_active=True,
        is_draft=False,
    )
    values.update(overrides)
    return Product(**values)


def test_feed_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("GOOGLE_MERCHANT_FEED_ENABLED", raising=False)
    assert is_google_merchant_feed_enabled() is False


@pytest.mark.parametrize("value", ["true", "yes", "on", "0", "2", " 1"])
def test_feed_gate_accepts_only_exact_one(monkeypatch, value):
    monkeypatch.setenv("GOOGLE_MERCHANT_FEED_ENABLED", value)
    assert is_google_merchant_feed_enabled() is False

    monkeypatch.setenv("GOOGLE_MERCHANT_FEED_ENABLED", "1")
    assert is_google_merchant_feed_enabled() is True


def test_feed_route_is_404_while_gated(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_MERCHANT_FEED_ENABLED", raising=False)
    assert client.get("/feeds/google-merchant.xml").status_code == 404


def test_feed_route_emits_valid_product(client, db, monkeypatch):
    monkeypatch.setenv("GOOGLE_MERCHANT_FEED_ENABLED", "1")
    product = _product()
    db.session.add(product)
    db.session.commit()

    response = client.get("/feeds/google-merchant.xml")

    assert response.status_code == 200
    assert response.mimetype == "application/rss+xml"
    assert response.headers["X-Robots-Tag"] == "noindex"
    root = ET.fromstring(response.get_data(as_text=True))
    item = root.find("./channel/item")
    assert item is not None
    assert item.find("g:id", GOOGLE_NS).text == str(product.id)
    assert item.find("g:price", GOOGLE_NS).text == "89.90 BRL"
    assert item.find("g:availability", GOOGLE_NS).text == "in_stock"
    assert item.find("g:condition", GOOGLE_NS).text == "new"


def test_feed_excludes_non_public_products(client, db, monkeypatch):
    monkeypatch.setenv("GOOGLE_MERCHANT_FEED_ENABLED", "1")
    db.session.add(_product(name="Inativo", is_active=False))
    db.session.add(_product(name="Rascunho", is_draft=True))
    db.session.commit()

    body = client.get("/feeds/google-merchant.xml").get_data(as_text=True)
    assert "Inativo" not in body
    assert "Rascunho" not in body


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": Decimal("0.00")},
        {"price": None},
        {"currency": "USD"},
        {"image1": "javascript:alert(1)"},
        {"image1": ""},
        {"name": "   "},
    ],
)
def test_feed_drops_invalid_items(req_ctx, overrides):
    xml = build_google_merchant_feed([_product(**overrides)])
    assert ET.fromstring(xml).find("./channel/item") is None


def test_feed_escapes_xml_and_does_not_expose_supplier_fields(req_ctx):
    product = _product(
        name="Vestido & verão",
        external_id="supplier-secret-123",
    )
    xml = build_google_merchant_feed([product])

    assert "Vestido &amp; verão" in xml
    assert "supplier-secret-123" not in xml
    assert "<g:brand>Pollo Modas</g:brand>" in xml


def test_feed_marks_zero_stock_as_out_of_stock(req_ctx):
    xml = build_google_merchant_feed([_product(stock=0)])
    item = ET.fromstring(xml).find("./channel/item")
    assert item.find("g:availability", GOOGLE_NS).text == "out_of_stock"


def test_feed_query_limit_is_fail_closed():
    with pytest.raises(ValueError, match="google_merchant_feed_limit_invalid"):
        find_google_merchant_products(limit=0)
