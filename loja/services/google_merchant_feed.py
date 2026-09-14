"""FASE 9.0.0-F — feed Google Merchant hard-off e somente leitura.

Gera RSS 2.0 com o namespace oficial do Google a partir de campos públicos do
catálogo. A rota permanece 404 até GOOGLE_MERCHANT_FEED_ENABLED ser exatamente
"1". Este módulo não cadastra feed, não chama o Google e não ativa campanha.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from flask import url_for

from loja.modules.models import Product
from loja.services.product_content import product_description_excerpt


ENABLED_ENV = "GOOGLE_MERCHANT_FEED_ENABLED"
_GOOGLE_NS = "http://base.google.com/ns/1.0"
_MAX_ITEMS = 5000
ET.register_namespace("g", _GOOGLE_NS)


def is_google_merchant_feed_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "") == "1"


def _public_http_url(value) -> str | None:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _price(product) -> str | None:
    if str(getattr(product, "currency", "BRL") or "BRL").upper() != "BRL":
        return None
    try:
        value = Decimal(str(product.price))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return f"{value.quantize(Decimal('0.01'))} BRL"


def find_google_merchant_products(*, limit: int = _MAX_ITEMS) -> list[Product]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_ITEMS:
        raise ValueError("google_merchant_feed_limit_invalid")
    return (
        Product.query
        .filter(
            Product.is_active.is_(True),
            Product.is_draft.is_(False),
        )
        .order_by(Product.created_at.asc())
        .limit(limit)
        .all()
    )


def _merchant_item(product) -> dict[str, str] | None:
    title = str(product.name or "").strip()
    image = _public_http_url(product.image1)
    price = _price(product)
    if not title or image is None or price is None:
        return None

    description = (
        product_description_excerpt(product.description or "")
        or title
    )[:5000]
    link = url_for(
        "main.produto_individual",
        id=str(product.id),
        _external=True,
    )
    return {
        "id": str(product.id),
        "title": title[:150],
        "description": description,
        "link": link,
        "image_link": image,
        "availability": "in_stock" if (product.stock or 0) > 0 else "out_of_stock",
        "price": price,
        "condition": "new",
        "brand": "Pollo Modas",
    }


def build_google_merchant_feed(products) -> str:
    """Serializa somente itens válidos; dados inválidos nunca viram preço zero."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Pollo Modas"
    ET.SubElement(channel, "link").text = url_for("main.home", _external=True)
    ET.SubElement(channel, "description").text = "Catálogo público Pollo Modas"

    for product in products:
        values = _merchant_item(product)
        if values is None:
            continue
        item = ET.SubElement(channel, "item")
        for key, value in values.items():
            ET.SubElement(item, f"{{{_GOOGLE_NS}}}{key}").text = value

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


__all__ = [
    "build_google_merchant_feed",
    "find_google_merchant_products",
    "is_google_merchant_feed_enabled",
]
