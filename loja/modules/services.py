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
from sqlalchemy import func

from loja import db
from loja.cache import get_cached, invalidate_cached, set_cached
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
from loja.utils import as_sao_paulo, utcnow


#: Posições válidas de foco (`object-position`) e de conteúdo do hero.
#: Valores fora da lista caem no padrão sem alterar o slide salvo.
HERO_FOCAL_POSITIONS = ("center", "top", "bottom", "left", "right")
HERO_CONTENT_POSITIONS = ("left", "center", "right")


def sanitize_hero_slide(slide) -> dict:
    """Projeção segura do slide para o template (nunca ORM vivo)."""
    focal_desktop = (getattr(slide, "focal_desktop", None) or "center").strip()
    if focal_desktop not in HERO_FOCAL_POSITIONS:
        focal_desktop = "center"
    focal_mobile = (getattr(slide, "focal_mobile", None) or "center").strip()
    if focal_mobile not in HERO_FOCAL_POSITIONS:
        focal_mobile = "center"
    content_position = (
        getattr(slide, "content_position", None) or "left"
    ).strip()
    if content_position not in HERO_CONTENT_POSITIONS:
        content_position = "left"
    return {
        "image": getattr(slide, "image", None),
        "image_mobile": getattr(slide, "image_mobile", None) or None,
        "alt": getattr(slide, "alt", None),
        "eyebrow": getattr(slide, "eyebrow", None),
        "title": getattr(slide, "title", None)
        if getattr(slide, "show_title", True) is not False else None,
        "description": (
            getattr(slide, "description", None)
            if getattr(slide, "show_description", True) is not False
            else None
        ),
        "link": getattr(slide, "link", None),
        "focal_desktop": focal_desktop,
        "focal_mobile": focal_mobile,
        "content_position": content_position,
    }


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

def deals_show_do_dia(promotions, now) -> bool:
    """True só se há ofertas e todas terminam hoje (America/Sao_Paulo).

    O flash "do dia" fica reservado para duração que sustenta a afirmação;
    promoções sem prazo ou com término futuro usam o título geral "Ofertas".
    """
    if not promotions:
        return False
    today = as_sao_paulo(now).date()
    return all(
        p.ends_at is not None and as_sao_paulo(p.ends_at).date() == today
        for p in promotions
    )


def get_home_data() -> dict:
    """
    Dados usados pela home (main/index.html).

    Estrutura retornada (snapshot puro: dicts/Decimal/datetime, NUNCA ORM
    vivo entre requests):
        hero_slides   – list[dict] (ativas, ordenadas; image/alt/eyebrow/title/link)
        categories    – cards de categoria
        banner        – dict  { quote, cta_text, cta_link }
        promotions    – list[dict] (ativas, não expiradas; cada card mostra
                        o próprio ends_at em America/Sao_Paulo quando houver)
        deals_do_dia  – bool (True só se todas as ofertas terminam hoje)
        products      – list[dict] (até 8, ativos, favoritados)
        testimonials  – list[dict] (visíveis, até 6)
        store_policy  – dict  { exchange_days }

    Cache (PR #103/TTFB): visitante ANÔNIMO usa cache em processo de 60s
    (mesmo mecanismo/TTL do site_config e do rodapé; limitação documentada:
    é por instância no Vercel). Usuário logado NUNCA usa o cache (favoritos
    são por usuário). Só o dicionário de dados é cacheado — nunca HTML,
    que conteria o token CSRF da sessão.
    """
    cacheavel = not current_user.is_authenticated
    if cacheavel:
        cached = get_cached("home_data")
        if cached is not None:
            return cached

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

    products = (
        public_product_query()
        .order_by(Product.created_at.desc())
        .limit(8)
        .all()
    )

    testimonials = (
        Testimonial.query
        .filter(Testimonial.is_visible == True)  # noqa: E712
        .order_by(Testimonial.created_at.desc())
        .limit(6)
        .all()
    )

    # Prazo real por oferta: cada card exibe o próprio ends_at (em
    # America/Sao_Paulo) quando ele existe. Não há relógio geral nem
    # fallback para o fim do dia — um prazo fabricado sugeriria que todas
    # as ofertas terminam juntas.
    deals_do_dia = deals_show_do_dia(promotions, now)

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

    data = {
        "structured_data": structured_data,
        "hero_slides": [
            sanitize_hero_slide(slide)
            for slide in hero_slides
        ],
        "categories": _get_home_categories(),
        "banner": {
            "quote": "A moda é a armadura para enfrentar a vida cotidiana.",
            "cta_text": "Ver a coleção completa",
            "cta_link": url_for("main.produtos"),
        },
        "promotions": [
            _snapshot_promotion(promo, favoritos) for promo in promotions
        ],
        "deals_do_dia": deals_do_dia,
        "products": [_snapshot_product(p, favoritos) for p in products],
        "testimonials": [
            {"author": t.author, "text": t.text, "rating": t.rating}
            for t in testimonials
        ],
        "store_policy": {
            "exchange_days": 7,
        },
    }
    if cacheavel:
        set_cached("home_data", data, ttl=60)
    return data


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


def _snapshot_product(p, favoritos):
    """Snapshot puro do produto para a home (dict de escalares, nunca ORM).

    Mantém somente os campos que o template usa. Decimal/str/bool preservam
    o tipo original: um JSON puro (str de preço) quebraria o format de preço
    e o cálculo de desconto no template.
    """
    return {
        "id": str(p.id),
        "name": p.name,
        "price": p.price,
        "image1": p.image1,
        "is_favorited": p.id in favoritos,
    }


def _snapshot_promotion(promo, favoritos):
    """Snapshot puro da promoção com o produto aninhado (nunca ORM vivo)."""
    product = promo.product
    return {
        "id": str(promo.id),
        "product_id": str(promo.product_id),
        "sale_price": promo.sale_price,
        "ends_at": promo.ends_at,
        "is_favorited": bool(promo.product_id) and promo.product_id in favoritos,
        "product": (
            None
            if product is None
            else {
                "id": str(product.id),
                "name": product.name,
                "price": product.price,
                "image1": product.image1,
            }
        ),
    }


# ── Categorias ────────────────────────────────────────────────────────────────

def _get_categories() -> list:
    return CategoryLink.query.all()


def _get_home_categories(limit=10):
    """
    Monta os cards de categoria da home (cat-strip / categories-grid).
    Usa uma imagem representativa (o produto mais recente daquela categoria).

    PR #103: o recorte acontece NO SQL — row_number() por category (ordenado
    por created_at desc) + filtro rn=1 + ORDER BY category + LIMIT — em vez
    de baixar todos os produtos ativos e filtrar no Python.
    """
    ranked = (
        db.session.query(
            Product.category.label("category"),
            Product.image1.label("image1"),
            func.row_number()
            .over(
                partition_by=Product.category,
                order_by=Product.created_at.desc(),
            )
            .label("rn"),
        )
        .filter(
            Product.is_active == True,  # noqa: E712
            Product.is_draft.is_(False),
            Product.category.isnot(None),
            Product.category != "",
        )
        .subquery()
    )
    rows = (
        db.session.query(ranked.c.category, ranked.c.image1)
        .filter(ranked.c.rn == 1)
        .order_by(ranked.c.category.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "slug": categoria,
            "label": categoria.replace("-", " ").title(),
            "image": imagem,
        }
        for categoria, imagem in rows
    ]


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


def invalidate_footer_cache() -> None:
    """Descarta o cache do rodapé após qualquer escrita dos seus links.

    Sem isso, um link salvo no admin só apareceria no rodapé (e na lista do
    próprio dashboard, que lê o mesmo cache) depois do TTL de 600s.
    """
    invalidate_cached("footer_data")


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