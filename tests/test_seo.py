"""FASE 9.0.0-D — SEO de produto, listagens e sitemap (somente leitura).

Cobre JSON-LD Product, URLs canônicas, meta de listagem/categoria, sitemap.xml
e o Sitemap dentro do robots.txt. Não indexa busca (noindex) nem vaza dados.
"""
import json
import uuid
from decimal import Decimal

import pytest

from loja.modules.models import Product
from loja.services.seo import (
    build_sitemap_urls,
    build_sitemap_xml,
    listing_canonical_url,
    listing_meta,
    product_canonical_url,
    product_structured_data,
)


@pytest.fixture()
def req_ctx(app):
    with app.test_request_context("/"):
        yield


def _product(**overrides):
    values = dict(
        id=uuid.uuid4(),
        name="Vestido Floral",
        description="<b>Vestido floral</b> para &ccedil;&atilde; dias quentes.",
        price=Decimal("89.90"),
        stock=10,
        image1="https://exemplo.com/vestido.jpg",
        category="feminino",
        is_active=True,
    )
    values.update(overrides)
    return Product(**values)


def test_product_structured_data_basic(req_ctx):
    product = _product()
    data = product_structured_data(product)
    assert data is not None
    parsed = json.loads(data)
    assert parsed["@type"] == "Product"
    assert parsed["name"] == "Vestido Floral"
    assert parsed["offers"]["price"] == "89.90"
    assert parsed["offers"]["priceCurrency"] == "BRL"
    assert parsed["offers"]["availability"] == "https://schema.org/InStock"
    assert "çã" in parsed["description"]
    assert "<b>" not in parsed["description"]


def test_product_structured_data_out_of_stock(req_ctx):
    data = product_structured_data(_product(stock=0))
    parsed = json.loads(data)
    assert parsed["offers"]["availability"] == "https://schema.org/OutOfStock"


def test_product_structured_data_images_from_gallery(req_ctx):
    product = _product()
    images = [{"url": "https://cdn.exemplo.com/a.jpg"}, {"url": "https://cdn.exemplo.com/b.jpg"}]
    data = product_structured_data(product, gallery_images=images)
    parsed = json.loads(data)
    assert parsed["image"] == ["https://cdn.exemplo.com/a.jpg", "https://cdn.exemplo.com/b.jpg"]


def test_product_structured_data_fallback_image(req_ctx):
    data = product_structured_data(_product())
    parsed = json.loads(data)
    assert "https://exemplo.com/vestido.jpg" in parsed["image"]


def test_product_structured_data_invalid_price_no_offers_price(req_ctx):
    parsed = json.loads(product_structured_data(_product(price=None)))
    assert "price" not in parsed["offers"]


def test_product_canonical_url(req_ctx):
    product = _product()
    url = product_canonical_url(product)
    assert f"/produtos/{product.id}" in url
    assert url.startswith("http")


def test_listing_canonical_basic(req_ctx):
    url = listing_canonical_url()
    assert url.endswith("/produtos")


def test_listing_canonical_with_categoria(req_ctx):
    url = listing_canonical_url(categoria="feminino")
    assert "categoria=feminino" in url


def test_listing_canonical_with_busca_ignores_params(req_ctx):
    url = listing_canonical_url(categoria="feminino", busca="vestido")
    assert "?" not in url


@pytest.mark.parametrize(
    ("categoria", "busca", "title_part", "robots"),
    [
        (None, None, "Produtos", "index,follow"),
        ("feminino", None, "feminino", "index,follow"),
        (None, "vestido", "Busca", "noindex,follow"),
        ("feminino", "vestido", "Busca", "noindex,follow"),
    ],
)
def test_listing_meta(req_ctx, categoria, busca, title_part, robots):
    meta = listing_meta(categoria=categoria, busca=busca)
    assert title_part.lower() in meta["title"].lower()
    assert meta["robots"] == robots


def test_sitemap_xml_escaping():
    xml = build_sitemap_xml(
        ["https://exemplo.com/produtos?categoria=verão&tamanho=M"]
    )
    assert "&amp;" in xml
    assert "urlset" in xml
    assert "<url>" in xml


def test_sitemap_xml_ignores_non_http():
    xml = build_sitemap_xml(["not-a-url", "https://ok.example/x"])
    assert "ok.example" in xml
    assert "not-a-url" not in xml


def test_sitemap_route_lists_public_products(client, db):
    product = _product()
    db.session.add(product)
    db.session.commit()
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.mimetype == "application/xml"
    body = response.get_data(as_text=True)
    assert f"/produtos/{product.id}" in body
    assert "/produtos" in body
    assert "/conta/" not in body


def test_sitemap_route_excludes_drafts_and_inactives(client, db):
    db.session.add(_product(is_active=False))
    db.session.add(_product(name="Rascunho", is_active=True, is_draft=True))
    db.session.commit()
    response = client.get("/sitemap.xml")
    body = response.get_data(as_text=True)
    assert "Rascunho" not in body


def test_robots_has_sitemap(client):
    response = client.get("/robots.txt")
    body = response.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Disallow: /conta/" in body
    assert "Sitemap:" in body
    assert "/sitemap.xml" in body


def test_product_page_has_canonical_and_structured_data(client, db):
    product = _product()
    db.session.add(product)
    db.session.commit()
    response = client.get(f"/produtos/{product.id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'rel="canonical"' in body
    assert f"/produtos/{product.id}" in body
    assert "application/ld+json" in body
    assert '"@type": "Product"' in body


def test_listing_page_canonical_and_meta(client, db):
    db.session.add(_product())
    db.session.commit()
    response = client.get("/produtos")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'rel="canonical"' in body
    assert "Produtos" in body


def test_category_page_canonical_with_param(client, db):
    db.session.add(_product())
    db.session.commit()
    response = client.get("/produtos?categoria=feminino")
    body = response.get_data(as_text=True)
    assert 'rel="canonical"' in body
    assert "categoria=feminino" in body


def test_search_page_is_noindex(client, db):
    db.session.add(_product())
    db.session.commit()
    response = client.get("/produtos?q=vestido")
    body = response.get_data(as_text=True)
    assert 'name="robots" content="noindex,follow"' in body