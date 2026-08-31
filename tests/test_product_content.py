"""Testes do sanitizador de HTML (bleach) e extrato para o storefront (Fase 7.2).

A descrição de produtos AliExpress pode conter HTML de terceiros; ela nunca deve
ser confiada como segura. Cobrimos: remoção de <script>/on*/javascript:, tags
básicas preservadas, remoção de atributos style (sem css_sanitizer), e extrato
em texto puro para <meta>/cards.
"""

from markupsafe import Markup

from loja.services.product_content import (
    build_product_gallery,
    product_description_excerpt,
    product_description_image_urls,
    sanitize_product_description_html,
    sanitize_product_description_without_images,
)


def test_sanitize_removes_script_and_handlers():
    raw = (
        "<p>Olá</p><script>alert(1)</script>"
        '<img src="x" onerror="alert(2)">'
        '<a href="javascript:alert(3)">link</a>'
        '<p onclick="alert(4)">texto</p>'
    )
    out = str(sanitize_product_description_html(raw))
    assert "<script" not in out
    assert "onclick" not in out
    assert "onerror" not in out
    assert "javascript:" not in out
    assert "<p>Olá</p>" in out
    assert "link" in out


def test_sanitize_keeps_basic_tags():
    raw = (
        "<p><strong>Legal</strong> <em>isso</em> e "
        '<a href="https://ex.com">ok</a></p>'
    )
    out = str(sanitize_product_description_html(raw))
    assert "<strong>Legal</strong>" in out
    assert "<em>isso</em>" in out
    assert 'href="https://ex.com"' in out
    assert "ok" in out


def test_sanitize_strips_style_attribute():
    out = str(sanitize_product_description_html('<p style="color:red">cor</p>'))
    assert "style" not in out


def test_sanitize_returns_markup_object():
    out = sanitize_product_description_html("<p>x</p>")
    assert isinstance(out, Markup)


def test_sanitize_empty_or_none():
    assert str(sanitize_product_description_html(None)) == ""
    assert str(sanitize_product_description_html("")) == ""


def test_sanitize_allows_img_with_src():
    # https://x/img.png é host EXTERNO -> atributo src removido
    out = str(sanitize_product_description_html('<img src="https://x/img.png" alt="foto">'))
    assert "src=" not in out
    assert "onerror" not in out


def test_sanitize_allows_aliexpress_cdn_images():
    raw = (
        '<img src="https://ae01.alicdn.com/kf/x.jpg" alt="a">'
        '<img src="https://ae-pic-a1.aliexpress-media.com/xyz.png" alt="b">'
    )
    out = str(sanitize_product_description_html(raw))
    assert 'src="https://ae01.alicdn.com/kf/x.jpg"' in out
    assert 'src="https://ae-pic-a1.aliexpress-media.com/xyz.png"' in out


def test_sanitize_rejects_non_alicdn_hosts_and_schemes():
    raws = [
        '<img src="http://ae01.alicdn.com/kf/x.jpg">',       # http
        '<img src="//ae01.alicdn.com/kf/x.jpg">',             # protocol-relative
        '<img src="data:image/png;base64,AAAA">',             # data:
        '<img src="javascript:alert(1)">',                    # javascript:
        '<img src="https://alicdn.com.evil.test/x.jpg">',     # contém, mas não é subdomínio
        '<img src="https://example.com/x.jpg">',              # host externo
        '<img src="https://not-alicdn.com/x.jpg">',           # prefixo falso
        '<img src="https://alicdn.com.evil.com/x.jpg">',      # sufixo falso
    ]
    for raw in raws:
        out = str(sanitize_product_description_html(raw))
        assert "src=" not in out, "deveria remover src de: %s" % raw


def test_sanitize_keeps_valid_aliexpress_subdomains():
    raw = (
        '<img src="https://ae01.alicdn.com/x.jpg">'
        '<img src="https://ae-pic-a1.aliexpress-media.com/x.png">'
    )
    out = str(sanitize_product_description_html(raw))
    assert 'src="https://ae01.alicdn.com/x.jpg"' in out
    assert 'src="https://ae-pic-a1.aliexpress-media.com/x.png"' in out


def test_sanitize_strips_handlers_on_img_and_keeps_allowed_attrs():
    # Mesmo com callback de img, onerror/onload não podem sobreviver; e alt/
    # title/width/height continuam permitidos.
    raw = (
        '<img src="https://ae01.alicdn.com/x.jpg" onerror="alert(1)" '
        'onload="evil()" alt="foto" width="120">'
    )
    out = str(sanitize_product_description_html(raw))
    assert 'src="https://ae01.alicdn.com/x.jpg"' in out
    assert 'onerror' not in out
    assert 'onload' not in out
    assert 'alt="foto"' in out
    assert 'width="120"' in out


def test_excerpt_strips_html_and_limits_words():
    raw = "<p>uma duas três quatro cinco</p>"
    exc = product_description_excerpt(raw, max_words=3)
    assert exc == "uma duas três…"
    assert "<p>" not in exc


def test_excerpt_handles_entities_and_none():
    assert product_description_excerpt("mais&nbsp;e&nbsp;mais &amp; tal") == "mais e mais & tal"
    assert product_description_excerpt(None) == ""
    assert product_description_excerpt("") == ""


# ── Fase 7.2.3: apresentação segura/responsiva da descrição ──────────────────

def _make_public(db, *, name, description, source="internal", image1="https://exemplo.com/img.png"):
    from decimal import Decimal
    from uuid import uuid4

    from loja.modules.models import Product

    product = Product(
        id=uuid4(),
        name=name,
        description=description,
        price=Decimal("99.90"),
        cost=Decimal("40.00"),
        image1=image1,
        source=source,
        is_active=True,
        is_draft=False,
        currency="BRL",
        target_margin_percent=Decimal("30.00"),
        pricing_status="priced" if source == "aliexpress" else "unpriced",
    )
    db.session.add(product)
    db.session.commit()
    return product


def _description_card_excerpt(client, product_name):
    from html import unescape

    resp = client.get("/produtos")
    assert resp.status_code == 200
    body = unescape(resp.get_data(as_text=True))
    idx = body.index(product_name)
    tail = body[idx: idx + 4000]
    p = tail.index("product-desc")
    start = tail.index(">", p) + 1
    end = tail.index("</p>", start)
    return tail[start:end].strip()


def test_card_image_only_description_falls_back_to_name(client, db):
    _make_public(
        db,
        name="Vestido Só Imagens",
        description='<img src="https://ae01.alicdn.com/kf/x.jpg" width="944">',
    )
    assert _description_card_excerpt(client, "Vestido Só Imagens") == "Vestido Só Imagens"


def test_card_empty_description_falls_back_to_name(client, db):
    _make_public(db, name="Sem Descrição", description=None)
    assert _description_card_excerpt(client, "Sem Descrição") == "Sem Descrição"


def test_card_textual_description_uses_excerpt(client, db):
    _make_public(
        db,
        name="Com Texto",
        description="<p>primeiro segundo terceiro quarto</p>",
    )
    given = _description_card_excerpt(client, "Com Texto")
    assert given == "primeiro segundo terceiro quarto"
    assert given != "Com Texto"
    assert "<" not in given


def test_aliexpress_image_heavy_description_not_rendered_twice(client, db):
    desc = (
        '<div style="width:944px">'
        '<p style="color:red">boas-vindas</p>'
        '<img src="https://ae01.alicdn.com/kf/1.jpg" onerror="alert(1)" width="944">'
        '<img src="https://ae-pic-a1.aliexpress-media.com/2.png" width="944">'
        "</div>"
    )
    product = _make_public(db, name="Ali Foto", description=desc, source="aliexpress")
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)
    # Imagens movidas para a galeria superior (topo), não na .details-text.
    assert 'data-gallery-src="https://ae01.alicdn.com/kf/1.jpg"' in body
    assert 'data-gallery-src="https://ae-pic-a1.aliexpress-media.com/2.png"' in body
    # A descrição completa sanitizada aparece UMA única vez (na .details-text),
    # agora SEM imagens — apenas o texto estrutural permanece.
    assert body.count("details-text") == 1
    assert "product-description-excerpt" in body
    # Conteúdo da região de descrição completa (fora do restante do layout).
    section_marker = '<div class="details-text">'
    start = body.index(section_marker) + len(section_marker)
    details_close = body.index("</section>", start)
    details_block = body[start:details_close]
    # Texto estrutural preservado; imagens removidas da descrição.
    assert "boas-vindas" in details_block
    assert "<img" not in details_block
    assert "ae01.alicdn.com/kf/1.jpg" not in details_block
    assert "aliexpress-media.com/2.png" not in details_block
    # style/onerror/peso do fornecedor removidos da descrição.
    assert "style=" not in details_block
    assert "onerror" not in details_block


def test_sanitized_description_keeps_valid_images_removes_style_and_onerror():
    raw = (
        '<img src="https://ae01.alicdn.com/kf/x.jpg" '
        'onerror="alert(1)" style="width:944px" width="944" height="10000" alt="ok">'
    )
    out = str(sanitize_product_description_html(raw))
    assert 'src="https://ae01.alicdn.com/kf/x.jpg"' in out
    assert "style" not in out
    assert "onerror" not in out
    assert 'alt="ok"' in out


def test_description_css_has_responsive_image_rules():
    css_path = "loja/static/styles/produtos_individual.css"
    with open(css_path, encoding="utf-8") as fh:
        css = fh.read()
    # Regras para imagens da descrição responsivas.
    assert "max-width:100% !important" in css
    assert "height:auto !important" in css
    assert "width:auto !important" in css
    # Nunca usa largura fixa do fornecedor.
    assert "width:944px" not in css
    # Tabelas/wrappers contidos (sem overflow horizontal).
    assert ".product-description table" in css or ".details-text table" in css


# ── Fase 7.2.4: galeria completa AliExpress ──────────────────────────────────

def _add_product_images(db, product, urls):
    from uuid import uuid4

    from loja.modules.models import ProductImage

    rows = [
        ProductImage(
            id=uuid4(),
            product_id=product.id,
            url=url,
            alt="Produto AliExpress",
            position=position,
            is_primary=position == 0,
        )
        for position, url in enumerate(urls)
    ]
    db.session.add_all(rows)
    db.session.commit()


HEADS = [
    "https://ae01.alicdn.com/kf/a%s.jpg",
    "https://ae-pic-a1.aliexpress-media.com/a%s.png",
]


def _desc_url(url):
    return f'<div style="width:944px"><img src="{url}" width="944"></div>'


# ── product_description_image_urls (política de segurança) ──────────────────

def test_description_image_urls_accepts_valid_cdns():
    desc = (
        '<img src="https://ae01.alicdn.com/ok.jpg">'
        '<img src="https://ae-pic-a1.aliexpress-media.com/ok.png">'
    )
    assert product_description_image_urls(desc) == [
        "https://ae01.alicdn.com/ok.jpg",
        "https://ae-pic-a1.aliexpress-media.com/ok.png",
    ]


def test_description_image_urls_rejects_unsafe_and_preserves_order_dedup():
    desc = (
        '<img src="https://ae01.alicdn.com/ok.jpg">'
        '<img src="http://ae01.alicdn.com/x.jpg">'
        '<img src="https://evil.example/x.jpg">'
        '<img src="https://alicdn.com.evil.test/x.jpg">'
        '<img src="javascript:alert(1)">'
        '<img src="data:image/png;base64,AAAA">'
        '<img src="https://ae01.alicdn.com/ok.jpg">'  # duplicata
    )
    assert product_description_image_urls(desc) == ["https://ae01.alicdn.com/ok.jpg"]


def test_description_image_urls_empty_and_none():
    assert product_description_image_urls(None) == []
    assert product_description_image_urls("") == []
    assert product_description_image_urls("<p>sem imagem</p>") == []


def test_description_sanitize_without_images_removes_img_keeps_text():
    raw = (
        '<div style="width:944px">'
        '<p>boas-vindas</p>'
        '<img src="https://ae01.alicdn.com/1.jpg" onerror="alert(1)" width="944">'
        "</div>"
    )
    out = str(sanitize_product_description_without_images(raw))
    assert "<img" not in out
    assert "<p>boas-vindas</p>" in out
    assert "style" not in out
    assert "onerror" not in out


# ── build_product_gallery (ordem, dedup, fontes) ────────────────────────────

def test_gallery_uses_all_product_images_not_just_four(db):
    product = _make_public(db, name="G1", description=None, source="aliexpress", image1=None)
    six = [HEADS[i % 2] % i for i in range(6)]
    _add_product_images(db, product, six)
    urls = [g["url"] for g in build_product_gallery(product)]
    assert urls == six
    assert len(urls) == 6


def test_gallery_product_images_plus_description_unique_urls(db):
    product = _make_public(db, name="G2", description="", source="aliexpress", image1=None)
    six = [HEADS[i % 2] % i for i in range(6)]
    _add_product_images(db, product, six)
    # 10 URLs únicas adicionais vindas só da descrição (6..15).
    desc_urls = [HEADS[i % 2] % i for i in range(6, 16)]
    product.description = "".join(_desc_url(u) for u in desc_urls)
    db.session.commit()

    urls = [g["url"] for g in build_product_gallery(product)]
    assert len(urls) == 16
    assert urls[:6] == six
    assert urls[6:] == desc_urls


def test_gallery_deduplicates_product_image_and_description(db):
    product = _make_public(db, name="G3", description="", source="aliexpress", image1=None)
    _add_product_images(db, product, [
        "https://ae01.alicdn.com/dup.jpg",
        "https://ae-pic-a1.aliexpress-media.com/dup.png",
    ])
    # Mesma URL da descrição que já está em ProductImage.
    product.description = _desc_url("https://ae01.alicdn.com/dup.jpg")
    db.session.commit()

    urls = [g["url"] for g in build_product_gallery(product)]
    assert urls == [
        "https://ae01.alicdn.com/dup.jpg",
        "https://ae-pic-a1.aliexpress-media.com/dup.png",
    ]


def test_gallery_description_only_for_aliexpress_not_internal(db):
    # Produto interno não importa imagens da descrição para a galeria.
    internal = _make_public(
        db,
        name="Interno",
        description=_desc_url("https://ae01.alicdn.com/x.jpg"),
        source="internal",
        image1=None,
    )
    assert build_product_gallery(internal) == []
    assert product_description_image_urls(internal.description)  # a extração existe


def test_gallery_legacy_image1_4_works_without_product_images(db):
    product = _make_public(
        db,
        name="Legado",
        description=None,
        source="internal",
        image1="https://exemplo.com/a.jpg",
    )
    # image1..4 continuam funcionando como antes (sem ProductImage).
    product.image2 = "https://exemplo.com/b.jpg"
    db.session.commit()
    urls = [g["url"] for g in build_product_gallery(product)]
    assert urls == ["https://exemplo.com/a.jpg", "https://exemplo.com/b.jpg"]


def test_gallery_never_emits_external_ids_or_skus(db):
    product = _make_public(db, name="G4", description="", source="aliexpress")
    _add_product_images(db, product, ["https://ae01.alicdn.com/1.jpg"])
    for g in build_product_gallery(product):
        assert "supplier" not in g
        assert "sku" not in g
        assert "external" not in g
        assert sorted(g) == ["alt", "url"]


# ── Integração: página renderizada ──────────────────────────────────────────

def _thumbs_of(body):
    import re

    return re.findall(r'data-gallery-src="([^"]+)"', body)


def test_page_gallery_counter_thumbs_and_first_active(client, db):
    product = _make_public(db, name="G5", description="", source="aliexpress", image1=None)
    six = [HEADS[i % 2] % i for i in range(6)]
    _add_product_images(db, product, six)
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)

    thumbs = _thumbs_of(body)
    assert thumbs == six  # número real de imagens nas thumbnails
    # Contador real: primeira thumbnail ativa e JS inicializa com o total.
    assert 'data-image-count="6"' in body
    assert 'class="product-gallery-thumb is-active"' in body
    # Nenhum SKU externo aparece no HTML.
    assert "supplier_sku_id" not in body
    assert "external_sku_id" not in body


def test_page_gallery_images_not_repeated_in_details(client, db):
    desc_urls = ["https://ae01.alicdn.com/kf/g%d.jpg" % i for i in (1, 2, 3)]
    desc = (
        "<p>texto da descrição</p>"
        + "".join(_desc_url(u) for u in desc_urls)
    )
    product = _make_public(db, name="G6", description=desc, source="aliexpress")
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)

    # Imagens entram na galeria...
    thumbs = _thumbs_of(body)
    assert desc_urls[0] in thumbs
    # ...mas NÃO aparecem de novo em .details-text.
    marker = '<div class="details-text">'
    start = body.index(marker) + len(marker)
    close = body.index("</section>", start)
    details = body[start:close]
    assert "<img" not in details
    assert "ae01.alicdn.com/kf/g1.jpg" not in details


def test_page_image_only_description_hides_empty_details(client, db):
    desc = "".join(_desc_url(u) for u in [
        "https://ae01.alicdn.com/only.jpg",
        "https://ae-pic-a1.aliexpress-media.com/only.png",
    ])
    product = _make_public(db, name="G7", description=desc, source="aliexpress")
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)

    # Sem bloco "Descrição" vazio.
    assert "details-text" not in body
    assert body.count("</section>") >= 1  # ainda há a seção de relacionados
    # Imagens na galeria.
    assert "ae01.alicdn.com/only.jpg" in body


def test_page_text_plus_images_text_in_description_images_in_gallery(client, db):
    desc = (
        "<p>texto útil aqui</p>"
        + _desc_url("https://ae01.alicdn.com/txt.jpg")
    )
    product = _make_public(db, name="G8", description=desc, source="aliexpress")
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)

    marker = '<div class="details-text">'
    start = body.index(marker) + len(marker)
    close = body.index("</section>", start)
    details = body[start:close]
    assert "texto útil aqui" in details
    assert "<img" not in details
    # Imagem ficou na galeria.
    assert "ae01.alicdn.com/txt.jpg" in body


def test_page_legacy_internal_product_still_shows_gallery(client, db):
    product = _make_public(
        db,
        name="Legado Interno",
        description="<p>ok</p>",
        source="internal",
    )
    assert product.image1 == "https://exemplo.com/img.png"
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)
    assert "https://exemplo.com/img.png" in body
    # Descrição interna continua presente na seção.
    assert "details-text" in body
