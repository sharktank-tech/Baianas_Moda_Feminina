"""PR #104 — fontes auto-hospedadas (plano Weblyzer, P3a).

Escopo aprovado:
- woff2 de Poppins (400..800) e DM Sans variável em ``public/static/fonts/``
  (espelho do CSS do Google; subset devanagari descartado — site PT-BR,
  o ``unicode-range`` nunca casa e o browser jamais o baixaria);
- ``@font-face`` locais em ``public/static/styles/fonts.css`` (regras
  verbatim do Google, só a URL mudou) com ``font-display: swap``;
- remoção do ``<link>`` de fonts.googleapis.com e dos dois preconnects.

Justificativa: o stylesheet externo era o único render-blocking da home
(FCP/LCP 6.3s no Weblyzer com TTFB 54ms).
"""

import re
from pathlib import Path

FONTS_DIR = Path("public/static/fonts")
FONTS_CSS = Path("public/static/styles/fonts.css")
BASE = Path("loja/templates/base.html")


def _blocks():
    css = FONTS_CSS.read_text(encoding="utf-8")
    return re.findall(r"@font-face\s*\{[^}]+\}", css)


def _weights(family):
    return {
        int(m.group(1))
        for b in _blocks()
        if ("font-family: '%s'" % family) in b
        for m in [re.search(r"font-weight:\s*(\d+)", b)]
        if m
    }


def test_home_has_no_google_font_reference(client):
    html = client.get("/").get_data(as_text=True)
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "styles/fonts.css" in html


def test_base_template_uses_local_fonts_and_no_font_preconnect():
    src = BASE.read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in src
    assert "fonts.gstatic.com" not in src
    assert "styles/fonts.css" in src
    assert not re.search(r'rel="preconnect"[^>]*fonts\.', src)


def test_fonts_css_only_local_src():
    css = FONTS_CSS.read_text(encoding="utf-8")
    srcs = re.findall(r"url\(([^)]+)\)", css)
    assert srcs, "fonts.css sem nenhuma url()"
    assert all(s.startswith("/static/fonts/") for s in srcs), srcs
    assert all(s.endswith(".woff2") for s in srcs), srcs
    assert not re.search(r"url\(\s*['\"]?https?://", css), "url() externa"


def test_every_referenced_font_file_exists_without_orphans():
    css = FONTS_CSS.read_text(encoding="utf-8")
    refs = {Path(s).name for s in re.findall(r"url\((/static/fonts/[^)]+)\)", css)}
    on_disk = {p.name for p in FONTS_DIR.glob("*.woff2")}
    assert refs <= on_disk, refs - on_disk
    assert on_disk - refs == set(), on_disk - refs
    assert len(on_disk) == 12, sorted(on_disk)


def test_font_files_are_valid_woff2():
    files = sorted(FONTS_DIR.glob("*.woff2"))
    assert len(files) == 12
    for p in files:
        assert p.read_bytes()[:4] == b"wOF2", p.name
    assert (FONTS_DIR / "OFL.txt").exists()


def test_poppins_weights_and_dm_sans_preserved_with_swap():
    blocks = _blocks()
    assert len(blocks) == 18, len(blocks)
    assert all(re.search(r"font-display:\s*swap", b) for b in blocks)
    assert _weights("Poppins") == {400, 500, 600, 700, 800}, _weights("Poppins")
    assert {300, 400, 500, 600} <= _weights("DM Sans"), _weights("DM Sans")
    assert not _weights("Poppins") & {300, 600} - {400, 500, 600, 700, 800}


def test_devanagari_subset_absent():
    css = FONTS_CSS.read_text(encoding="utf-8")
    assert "/* devanagari */" not in css
