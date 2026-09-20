"""
services.py
~~~~~~~~~~~
Camada de serviços da aplicação.

Responsabilidade: buscar, montar e retornar dados prontos para as rotas,
mantendo as views limpas de lógica de negócio e consultas diretas ao banco.

"""
import json

from flask import has_request_context, url_for
from flask_login import current_user

from loja import db
from loja.cache import get_cached, set_cached
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
    ContactInfo,
    Favorite,
)
from loja.modules.footer_links import is_safe_footer_url
from loja.utils import utcnow


# ── Layout base (contexto global de admin) ─────────────────────────────────────

def get_layout_data() -> dict:
    """
    Contexto base usado pelo blueprint admin (base.html).

    Estrutura retornada:
        site          – SiteConfig | None
        categories    – list[CategoryLink]
        hero_slides   – list[HeroSlide]
        products      – list[Product]  (ativos, mais recentes)
        promotions    – list[Promotion] (ativas)
        testimonials  – list[Testimonial] (visíveis)
        banner        – dict  { quote, cta_text }
        promo_badge   – str
        footer        – dict  (ver get_footer_data)
    """
    site = SiteConfig.query.first()

    return {
        "site":         site,
        "categories":   _get_categories(),
        "hero_slides":  _get_hero_slides(),
        "products":     _get_active_products(),
        "promotions":   _get_active_promotions(),
        "testimonials": _get_visible_testimonials(),
        "banner":       _build_banner(site),
        "promo_badge":  site.promo_badge if site else "",
        "footer":       get_footer_data(),
    }


# ── Home ──────────────────────────────────────────────────────────────────────

def get_home_data() -> dict:
    """
    Dados usados pela home (main/index.html).

    Estrutura retornada:
        hero_slides   – list[HeroSlide] (ativas, ordenadas)
        categories    – cards de categoria
        banner        – dict  { quote, cta_text, cta_link }
        promotions    – list[Promotion] (ativas, não expiradas; cada card mostra
                        o próprio ends_at quando houver prazo real)
        products      – list[Product] (até 8, ativos, favoritados)
        testimonials  – list[Testimonial] (visíveis, até 6)
        store_policy  – dict  { exchange_days, max_installments, free_shipping_min }
    """
    favoritos = get_favorited_ids()

    hero_slides = (
        HeroSlide.query
        .filter(HeroSlide.is_active == True)  # noqa: E712
        .order_by(HeroSlide.order.asc())
        .all()
    )

    # Ofertas do dia: vêm de Promotion (Product não tem sale_price/original_price).
    # Promoções só enxergam produtos PÚBLICOS (drafts nunca aparecem).
    now = utcnow()
    public_product_ids = (
        db.session.query(Product.id)
        .filter(
            Product.is_active.is_(True),
            Product.is_draft.is_(False),
        )
        .subquery()
    )
    promotions = (
        Promotion.query
        .filter(
            Promotion.is_active == True,  # noqa: E712
            db.or_(Promotion.ends_at.is_(None), Promotion.ends_at > now),
            db.or_(
                Promotion.product_id.is_(None),
                Promotion.product_id.in_(public_product_ids),
            ),
        )
        .order_by(Promotion.created_at.desc())
        .limit(8)
        .all()
    )
    for promo in promotions:
        promo.is_favorited = bool(promo.product_id) and promo.product_id in favoritos

    products = (
        public_product_query()
        .order_by(Product.created_at.desc())
        .limit(8)
        .all()
    )
    products = [_serialize_product(p, favoritos) for p in products]

    testimonials = (
        Testimonial.query
        .filter(Testimonial.is_visible == True)  # noqa: E712
        .order_by(Testimonial.created_at.desc())
        .limit(6)
        .all()
    )

    # Prazo real por oferta: cada card exibe o próprio ends_at quando ele
    # existe. Não há relógio geral nem fallback para o fim do dia — um
    # prazo fabricado sugeriria que todas as ofertas terminam juntas.

    structured_data = None
    if products and has_request_context():
        item_list = {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": idx + 1,
                    "url": url_for(
                        "main.produto_individual",
                        id=str(p.id),
                        _external=True,
                    ),
                }
                for idx, p in enumerate(products)
            ],
        }
        structured_data = json.dumps(item_list, ensure_ascii=False)

    return {
        "structured_data": structured_data,
        "hero_slides": hero_slides,
        "categories": _get_home_categories(),
        "banner": {
            "quote": "A moda é a armadura para enfrentar a vida cotidiana.",
            "cta_text": "Ver a coleção completa",
            "cta_link": url_for("main.produtos"),
        },
        "promotions": promotions,
        "products": products,
        "testimonials": testimonials,
        "store_policy": {
            "exchange_days": 30,
            "max_installments": 12,
            "free_shipping_min": 249,
        },
    }


def public_product_query():
    """Query base de produtos PUBLICAMENTE visíveis.

    Centraliza o guarda de visibilidade: um produto só é público se estiver
    ativo E não for rascunho. Não depender apenas de template para esconder
    drafts (AliExpress não precificado jamais vaza publicamente).
    """
    return Product.query.filter(
        Product.is_active.is_(True),
        Product.is_draft.is_(False),
    )


def get_favorited_ids() -> set:
    """IDs de produtos favoritados pelo usuário logado. Anônimo = nenhum."""
    if not current_user.is_authenticated:
        return set()
    rows = (
        db.session.query(Favorite.product_id)
        .filter(Favorite.user_id == current_user.id)
        .all()
    )
    return {r[0] for r in rows}


def _serialize_product(p, favoritos):
    p.is_favorited = p.id in favoritos
    return p


# ── Categorias ────────────────────────────────────────────────────────────────

def _get_categories() -> list:
    return CategoryLink.query.all()


def _get_home_categories(limit=10):
    """
    Monta os cards de categoria da home (cat-strip / categories-grid).
    Usa uma imagem representativa (o produto mais recente daquela categoria).
    """
    rows = (
        db.session
        .query(Product.category, Product.image1)
        .filter(
            Product.is_active == True,  # noqa: E712
            Product.is_draft.is_(False),
            Product.category.isnot(None),
            Product.category != ''
        )
        .order_by(Product.category.asc(), Product.created_at.desc())
        .all()
    )
    vistas, categorias = set(), []
    for categoria, imagem in rows:
        if categoria in vistas:
            continue
        vistas.add(categoria)
        categorias.append({
            "slug": categoria,
            "label": categoria.replace("-", " ").title(),
            "image": imagem,
        })
        if len(categorias) >= limit:
            break
    return categorias


# ── Helpers privados ──────────────────────────────────────────────────────────

def _get_hero_slides() -> list:
    return HeroSlide.query.all()


def _get_active_products(limit: int = 8) -> list:
    return (
        public_product_query()
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


def get_footer_data() -> dict:
    """Dados do rodapé, baseado nas configurações da loja.

    Cacheado em memória: é consultado em todas as páginas (context
    processor global) e raramente muda.
    """
    cached = get_cached("footer_data")
    if cached is not None:
        return cached

    site = SiteConfig.query.first()
    footer_config = FooterConfig.query.first()

    def _safe(rows):
        # Omite registros antigos inválidos na renderização sem apagar dados:
        # o admin deve revisar/excluir pelo dashboard.
        return [r for r in rows if is_safe_footer_url(getattr(r, "url", ""))]

    data = {
        "brand_name":        footer_config.brand_name        if footer_config else "",
        "brand_description": footer_config.brand_description if footer_config else "",
        "social_links":      _safe(SocialLink.query.all()),
        "nav_links":         _safe(NavLink.query.all()),
        "categories":        _safe(CategoryLink.query.all()),
        "contact":           ContactInfo.query.first(),
        "copyright_year":    site.copyright_year if site else 2025,
        "copyright_text":    site.copyright_text if site else "",
        "legal_links":       _safe(LegalLink.query.all()),
    }
    set_cached("footer_data", data, ttl=600)
    return data


# ── Produtos ──────────────────────────────────────────────────────────────────

def get_products_page_data() -> dict:
    """
    Contexto para main/produtos.html.
    Expandir conforme a página evoluir (filtros, paginação, categorias…).
    """
    return {
        "products": _get_active_products(limit=None),
    }