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
    parsed = data
    assert parsed["@type"] == "Product"
    assert parsed["name"] == "Vestido Floral"
    assert parsed["offers"]["price"] == "89.90"
    assert parsed["offers"]["priceCurrency"] == "BRL"
    assert parsed["offers"]["availability"] == "https://schema.org/InStock"
    assert "çã" in parsed["description"]
    assert "<b>" not in parsed["description"]


def test_product_structured_data_out_of_stock(req_ctx):
    data = product_structured_data(_product(stock=0))
    parsed = data
    assert parsed["offers"]["availability"] == "https://schema.org/OutOfStock"


def test_product_structured_data_images_from_gallery(req_ctx):
    product = _product()
    images = [{"url": "https://cdn.exemplo.com/a.jpg"}, {"url": "https://cdn.exemplo.com/b.jpg"}]
    data = product_structured_data(product, gallery_images=images)
    parsed = data
    assert parsed["image"] == ["https://cdn.exemplo.com/a.jpg", "https://cdn.exemplo.com/b.jpg"]


def test_product_structured_data_fallback_image(req_ctx):
    data = product_structured_data(_product())
    parsed = data
    assert "https://exemplo.com/vestido.jpg" in parsed["image"]


def test_product_structured_data_invalid_price_no_offers_price(req_ctx):
    parsed = product_structured_data(_product(price=None))
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


def test_product_json_ld_escapes_script_terminator(client, db):
    product = _product(name='Vestido </script><script>alert(1)</script>')
    db.session.add(product)
    db.session.commit()

    response = client.get(f"/produtos/{product.id}")
    body = response.get_data(as_text=True)

    assert "</script><script>alert(1)</script>" not in body
    assert "\\u003c/script\\u003e" in body


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


def test_product_title_uses_name_and_description_not_duplicated(client, db):
    """Verify product title uses product.name and description section shows only description.

    Confirms the template correctly separates name from description:
    - <h1 class="product-name"> displays product.name
    - Meta description uses product_description_excerpt (text-only, not the name)
    - The structured data name matches product.name
    - The description in structured data is not the same as the name
    """
    from uuid import uuid4
    from decimal import Decimal

    name = "Camisa Listrada Azul"
    descricao = "Uma camisa de algodão 100% azul com listras brancas. "
    descricao += "Possui botões de madrepérola e manga longa ajustada."

    product = Product(
        id=uuid4(),
        name=name,
        description=descricao,
        price=Decimal("49.90"),
        stock=15,
        image1="https://exemplo.com/camisa.jpg",
        category="masculino",
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()

    # 1) Structured data: name equals product.name
    from loja.services.seo import product_structured_data
    data = product_structured_data(product)
    assert data is not None
    assert data["@type"] == "Product"
    assert data["name"] == name, "Structured data name must equal product.name"

    # 2) Structured data: description is NOT the same as the name
    assert data["description"] != name, (
        "Structured data description must not be the product name"
    )
    # description must contain actual description text
    assert descricao[:20] in data["description"], (
        "Structured data description must contain description text"
    )

    # 3) Template: <h1 class="product-name"> contains the name (not description)
    from flask import render_template
    html = render_template("main/produto_individual.html", product=product)
    assert 'class="product-name"' in html, (
        "Template must have h1 with class product-name"
    )
    # The h1 content should be the name
    assert name in html, "h1 must contain product.name"

    # 4) Meta description (block meta_description) uses excerpt, not raw name
    # The meta_description block is: {{ product_description_excerpt(product.description) or product.name }}
    # Since we have description text, excerpt should be used
    from loja.services.product_content import product_description_excerpt
    with __import__("warnings").catch_warnings():
        import warnings
        warnings.filterwarnings("ignore")
        excerpt = product_description_excerpt(descricao, max_words=40)
    # excerpt must not equal the full name
    assert excerpt != name, "Excerpt must not equal the full product name"
    assert len(excerpt) > 0, "Excerpt must have content"

    # 5) Description section in template uses sanitized description, not the name
    with __import__("warnings").catch_warnings():
        warnings.filterwarnings("ignore")
        desc_html = render_template(
            "main/produto_individual.html", product=product,
            uses_variants=False,
        )
    assert 'class="product-description"' in desc_html, (
        "Template must have .product-description section"
    )
    # Description section must contain description text, not just the name
    assert descricao[:30] in desc_html, (
        "Description section must contain description text"
    )

    # 6) Listing page: excerpt uses description, not name
    with __import__("warnings").catch_warnings():
        warnings.filterwarnings("ignore")
        list_excerpt = product_description_excerpt(descricao, max_words=30)
    assert list_excerpt != name, "Listing excerpt must not equal product name"
    assert descricao[:20] in list_excerpt, (
        "Listing excerpt must contain description text"
    )