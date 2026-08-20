"""
services.py
~~~~~~~~~~~
Camada de serviços da aplicação.

Responsabilidade: buscar, montar e retornar dados prontos para as rotas,
mantendo as views limpas de lógica de negócio e consultas diretas ao banco.

"""
from loja.modules.models import (
    Product,
    HeroSlide,
    Promotion,
    Testimonial,
    SiteConfig,
    FooterConfig,
    SocialLink,
    NavLink,
    CategoryLink,
    LegalLink,
    ContactInfo
)


# ── Home ──────────────────────────────────────────────────────────────────────

def get_home_data() -> dict:
    """
    Retorna todo o contexto necessário para renderizar base.html.

    Estrutura retornada:
        site          – SiteConfig | None
        hero_slides   – list[HeroSlide]
        products      – list[Product]  (até 8, ativos, mais recentes)
        promotions    – list[Promotion] (ativas)
        testimonials  – list[Testimonial] (visíveis, até 3)
        banner        – dict  { quote, cta_text }
        promo_badge   – str
        footer        – dict  (ver _build_footer)
    """
    site = SiteConfig.query.first()

    return {
        "site":         site,
        "categories": _get_categories(),
        "hero_slides":  _get_hero_slides(),
        "products":     _get_active_products(),
        "promotions":   _get_active_promotions(),
        "testimonials": _get_visible_testimonials(),
        "banner":       _build_banner(site),
        "promo_badge":  site.promo_badge if site else "",
        "footer":       _build_footer(site),
    }

# ── Categorias ────────────────────────────────────────────────────────────────

def _get_categories() -> list:
    return CategoryLink.query.all()

# ── Helpers privados ──────────────────────────────────────────────────────────

def _get_hero_slides() -> list:
    return HeroSlide.query.all()


def _get_active_products(limit: int = 8) -> list:
    return (
        Product.query
        .filter_by(is_active=True)
        .order_by(Product.created_at.desc())
        .limit(limit)
        .all()
    )


def _get_active_promotions() -> list:
    return Promotion.query.filter_by(is_active=True).all()


def _get_visible_testimonials(limit: int = 3) -> list:
    return (
        Testimonial.query
        .filter_by(is_visible=True)
        .limit(limit)
        .all()
    )


def _build_banner(site: "SiteConfig | None") -> dict:
    return {
        "quote":    site.banner_quote    if site else "",
        "cta_text": site.banner_cta_text if site else "",
    }


def _build_footer(site: "SiteConfig | None") -> dict:
    footer_config = FooterConfig.query.first()
    return {
        "brand_name":        footer_config.brand_name        if footer_config else "",
        "brand_description": footer_config.brand_description if footer_config else "",
        "social_links":      SocialLink.query.all(),
        "nav_links":         NavLink.query.all(),
        "categories":        CategoryLink.query.all(),
        "contact":           ContactInfo.query.first(),
        "copyright_year":    site.copyright_year if site else 2025,
        "copyright_text":    site.copyright_text if site else "",
        "legal_links":       LegalLink.query.all(),
    }


# ── Produtos ──────────────────────────────────────────────────────────────────

def get_products_page_data() -> dict:
    """
    Contexto para main/produtos.html.
    Expandir conforme a página evoluir (filtros, paginação, categorias…).
    """
    return {
        "products": _get_active_products(limit=None),
    }
