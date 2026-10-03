"""Prioridade 2 — performance/asset do storefront (plano Weblyzer).

Mudanças aprovadas do PR de Core Web Vitals:
- ``defer`` no ``<script>`` do Tailwind CDN (render-blocking);
- ``loading="lazy" decoding="async"`` nas 5 imgs do storefront que faltavam
  (4 do rodapé + lightbox do produto);
- pesos das fontes preservados (decisão: manter Poppins 400..800 — o corte
  de pesos mudaria o visual da navbar).

PR #104 (fontes auto-hospedadas) substituiu o ``preconnect``/``<link>`` de
fonts.googleapis.com por ``static/styles/fonts.css`` local — as invariantes
de fonte agora apontam para o CSS local (ver ``test_font_self_hosted.py``).

Invariantes checados no HTML renderizado (home e detalhe do produto).
"""

import re
from pathlib import Path


def _imgs(html):
    return re.findall(r"<img\b[^>]*>", html, re.S)


def _missing_lazy(html):
    return [
        tag
        for tag in _imgs(html)
        if "loading=" not in tag and "fetchpriority=" not in tag
    ]


def test_home_has_local_fonts_and_deferred_tailwind(client):
    html = client.get("/").get_data(as_text=True)
    assert "styles/fonts.css" in html
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert '<script src="https://cdn.tailwindcss.com" defer></script>' in html


def test_font_weights_preserved_as_decided():
    """Regressão da decisão 'manter todos os pesos' (visual da navbar).

    Após o PR #104 os pesos vivem em ``fonts.css`` local (mesmos woff2).
    """
    css = Path("loja/static/styles/fonts.css").read_text(encoding="utf-8")
    blocks = re.findall(r"@font-face\s*\{[^}]+\}", css)
    poppins = {
        int(m.group(1))
        for b in blocks
        if "font-family: 'Poppins'" in b
        for m in [re.search(r"font-weight:\s*(\d+)", b)]
        if m
    }
    assert poppins == {400, 500, 600, 700, 800}, poppins
    assert all(re.search(r"font-display:\s*swap", b) for b in blocks)


def test_every_home_img_has_lazy_or_fetchpriority(client):
    html = client.get("/").get_data(as_text=True)
    assert _imgs(html), "home renderizada sem imgs"
    assert _missing_lazy(html) == []
    assert any('fetchpriority="high"' in tag for tag in _imgs(html))


def test_product_page_imgs_follow_invariant_and_keep_lcp_priority(
    client, product
):
    resp = client.get(f"/produtos/{product.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _imgs(html), "pagina de produto sem imgs"
    assert _missing_lazy(html) == []
    # A imagem principal (LCP) continua prioritária.
    assert any('fetchpriority="high"' in tag for tag in _imgs(html))


def test_base_template_imgs_all_lazy():
    """Direto na fonte: todo <img> de base.html (rodapé) tem lazy."""
    from pathlib import Path

    src = Path("loja/templates/base.html").read_text()
    tags = re.findall(r"<img\b[^>]*>", src, re.S)
    assert len(tags) >= 4
    missing = [
        tag
        for tag in tags
        if "loading=" not in tag and "fetchpriority=" not in tag
    ]
    assert missing == [], missing
