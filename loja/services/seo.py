"""FASE 9.0.0-D — SEO de produto, listagens e sitemap (somente leitura).

Constrói dados estruturados (JSON-LD, schema.org) e URLs canônicas a partir de
campos já sanitizados do produto. Nenhum payload bruto, ID de fornecedor ou
SKU externo vaza: tudo é extraído de fields públicos e texto-plano.
"""

from __future__ import annotations

import json
from typing import Optional

from flask import request, url_for

from loja.services.product_content import (
    build_product_gallery,
    product_description_excerpt,
)

_SITE_CURRENCY = "BRL"


def _price_to_float(product) -> Optional[float]:
    try:
        return round(float(product.price), 2)
    except (TypeError, ValueError):
        return None


def _image_urls(product, gallery_images) -> list[str]:
    images = list(gallery_images) if gallery_images else []
    if not images:
        urls = [getattr(product, f"image{i}", None) for i in range(1, 5)]
        images = [{"url": url} for url in urls if url]
    return [img["url"] for img in images if img.get("url")]


def product_structured_data(product, *, gallery_images=None, request=None) -> Optional[str]:
    """JSON-LD Product (schema.org) com dados públicos e texto-plano.

    Retorna a string JSON (prontas para template via ``| safe`` seguindo o
    padrão da home) ou None quando o produto não é indexável.
    """
    excerpt = product_description_excerpt(product.description or "") or product.name
    price = _price_to_float(product)
    images = _image_urls(product, gallery_images)

    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": (product.name or "").strip()[:200],
        "description": excerpt[:500],
        "image": images[:10],
    }

    product_url = url_for(
        "main.produto_individual",
        id=str(product.id),
        _external=True,
    )
    data["url"] = product_url

    availability = (
        "https://schema.org/InStock"
        if (getattr(product, "stock", 0) or 0) > 0
        else "https://schema.org/OutOfStock"
    )
    offers = {
        "@type": "Offer",
        "priceCurrency": _SITE_CURRENCY,
        "availability": availability,
        "url": product_url,
    }
    if price is not None:
        offers["price"] = f"{price:.2f}"
    data["offers"] = offers

    return json.dumps(data, ensure_ascii=False)


def product_canonical_url(product) -> str:
    """URL absoluta canônica do produto (sem rastreio/UTM)."""
    return url_for(
        "main.produto_individual",
        id=str(product.id),
        _external=True,
    )


def listing_canonical_url(
    *,
    categoria: Optional[str] = None,
    busca: Optional[str] = None,
) -> str:
    """URL canônica de /produtos.

    Listagens são indexáveis apenas pela categoria (com filtro de busca a
    página não é canônica). Query strings repetidas não geram URLs duplicadas.
    """
    if busca:
        return url_for("main.produtos", _external=True)
    if categoria:
        return url_for("main.produtos", categoria=categoria, _external=True)
    return url_for("main.produtos", _external=True)


def listing_meta(categoria: Optional[str] = None, busca: Optional[str] = None) -> dict:
    """title/meta_description/robots por listagem.

    Categoria tem título próprio (SEO); busca não é indexável (noindex),
    evitando páginas duplicadas por termos de pesquisa.
    """
    if busca:
        return {
            "title": "Busca — Pollo Modas",
            "meta_description": "Busca de produtos na Pollo Modas.",
            "robots": "noindex,follow",
            "canonical": listing_canonical_url(busca=busca),
        }
    if categoria:
        return {
            "title": f"{categoria} — Pollo Modas",
            "meta_description": f"Roupas e acessórios de {categoria} na Pollo Modas.",
            "robots": "index,follow",
            "canonical": listing_canonical_url(categoria=categoria),
        }
    return {
        "title": "Produtos — Pollo Modas",
        "meta_description": "Conheça a coleção completa de roupas e acessórios da Pollo Modas.",
        "robots": "index,follow",
        "canonical": listing_canonical_url(),
    }


def build_sitemap_urls() -> list[str]:
    """URLs públicas indexáveis do sitemap (produtos + páginas institucionais).

    Somente produtos ativos e não rascunho (mesmo guarda do site público).
    Uso de ``request.url_root`` para montar absolutas sem depender de config.
    """
    from loja.modules.models import Product

    root = request.url_root.rstrip("/")

    static_urls = [
        url_for("main.home", _external=True),
        url_for("main.produtos", _external=True),
        url_for("main.contato", _external=True),
        url_for("main.sobre", _external=True),
        url_for("main.politica_privacidade", _external=True),
        url_for("main.condi_uso", _external=True),
    ]

    products = (
        Product.query
        .filter(
            Product.is_active.is_(True),
            Product.is_draft.is_(False),
        )
        .order_by(Product.created_at.asc())
        .all()
    )

    product_urls = [
        url_for("main.produto_individual", id=str(product.id), _external=True)
        for product in products
    ]
    return static_urls + product_urls


def build_sitemap_xml(urls: list[str]) -> str:
    """Serializa URLs como sitemap.xml (apenas loc, sem frequência/prioridade).

    Entradas são sempre absolutas e sem caracteres de controle; XML é escapado
    de forma mínima e segura.
    """
    from xml.sax.saxutils import escape

    items = "\n".join(
        f"  <url><loc>{escape(url)}</loc></url>"
        for url in urls
        if url.startswith("http")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>\n"
    )


__all__ = [
    "build_sitemap_urls",
    "build_sitemap_xml",
    "listing_canonical_url",
    "listing_meta",
    "product_canonical_url",
    "product_structured_data",
]