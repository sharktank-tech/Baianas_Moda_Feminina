"""Testes do sanitizador de HTML (bleach) e extrato para o storefront (Fase 7.2).

A descrição de produtos AliExpress pode conter HTML de terceiros; ela nunca deve
ser confiada como segura. Cobrimos: remoção de <script>/on*/javascript:, tags
básicas preservadas, remoção de atributos style (sem css_sanitizer), e extrato
em texto puro para <meta>/cards.
"""

from markupsafe import Markup

from loja.services.product_content import (
    product_description_excerpt,
    sanitize_product_description_html,
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

def _make_public(db, *, name, description, source="internal"):
    from decimal import Decimal
    from uuid import uuid4

    from loja.modules.models import Product

    product = Product(
        id=uuid4(),
        name=name,
        description=description,
        price=Decimal("99.90"),
        cost=Decimal("40.00"),
        image1="https://exemplo.com/img.png",
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
    # Descrição completa sanitizada aparece UMA única vez (na .details-text).
    assert body.count("details-text") == 1
    assert "product-description-excerpt" in body
    # Conteúdo da região de descrição completa (fora do restante do layout).
    section_marker = '<div class="details-text">'
    start = body.index(section_marker) + len(section_marker)
    details_start = body.index("<p", start)
    details_close = body.index("</section>", start)
    details_block = body[start:details_close]
    # Imagens permitidas dos CDNs AliExpress preservadas.
    assert "ae01.alicdn.com/kf/1.jpg" in details_block
    assert "aliexpress-media.com/2.png" in details_block
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
