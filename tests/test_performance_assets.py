"""Prioridade 2 — performance/asset do storefront (plano Weblyzer).

Mudanças aprovadas do PR de Core Web Vitals:
- ``preconnect`` para ``fonts.gstatic.com`` (só o googleapis existia);
- ``defer`` no ``<script>`` do Tailwind CDN (render-blocking);
- ``loading="lazy" decoding="async"`` nas 5 imgs do storefront que faltavam
  (4 do rodapé + lightbox do produto);
- pesos das fontes preservados (decisão: manter Poppins 400..800 — o corte
  de pesos mudaria o visual da navbar).

Invariantes checados no HTML renderizado (home e detalhe do produto).
"""

import re


def _imgs(html):
    return re.findall(r"<img\b[^>]*>", html, re.S)


def _missing_lazy(html):
    return [
        tag
        for tag in _imgs(html)
        if "loading=" not in tag and "fetchpriority=" not in tag
    ]


def test_home_has_gstatic_preconnect_and_deferred_tailwind(client):
    html = client.get("/").get_data(as_text=True)
    assert (
        'rel="preconnect" href="https://fonts.gstatic.com" crossorigin' in html
    )
    assert '<script src="https://cdn.tailwindcss.com" defer></script>' in html


def test_font_weights_preserved_as_decided(client):
    """Regressão da decisão 'manter todos os pesos' (visual da navbar)."""
    html = client.get("/").get_data(as_text=True)
    assert "Poppins:wght@400;500;600;700;800" in html
    assert "9..40,300" in html
    assert "display=swap" in html


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
