"""P3b — Tailwind Play CDN → CSS pré-compilado (plano Weblyzer).

Mudança aprovada (opção: package.json + CSS gerado commitado):
- remove o render/eval do ``cdn.tailwindcss.com`` (terceiro-party, era a
  única fonte de ``'unsafe-eval'`` da CSP e contribuía pro DCL);
- gera ``loja/static/styles/tailwind.css`` versionado (Vercel sem Node);
- ``package.json`` + ``tailwind.config.js`` + ``src/tailwind.input.css``
  documentam o build (binário standalone ou npx).

Invariantes: nenhum template referencia o CDN, o CSS local existe e cobre
todos os templates apontados pela config.
"""

import re
from pathlib import Path

CONFIG = Path("tailwind.config.js")
INPUT = Path("src/tailwind.input.css")
OUTPUT = Path("loja/static/styles/tailwind.css")
PKG = Path("package.json")
TEMPLATES = Path("loja/templates")


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_home_no_cdn_and_links_local_css(client):
    html = client.get("/").get_data(as_text=True)
    assert "cdn.tailwindcss.com" not in html
    assert "styles/tailwind.css" in html


def test_no_template_references_cdn():
    offenders = [
        str(p)
        for p in _templates()
        if "cdn.tailwindcss.com" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], offenders


def test_generated_css_exists_and_is_substantial():
    assert OUTPUT.exists(), "tailwind.css nao gerado (roda o build)"
    size = OUTPUT.stat().st_size
    assert 8_000 <= size <= 200_000, size
    css = OUTPUT.read_text(encoding="utf-8")
    for probe in ("flex", "hidden", "bg-pink-600", "md\\:"):
        assert probe in css, probe


def test_config_content_covers_every_template():
    cfg = CONFIG.read_text(encoding="utf-8")
    assert '"./loja/templates/**/*.html"' in cfg
    files = _templates()
    assert len(files) >= 60, len(files)
    # todo template esta sob o padrao content da config
    assert all(str(p).startswith("loja/templates/") for p in files)


def test_build_inputs_documented():
    assert INPUT.exists()
    directives = INPUT.read_text(encoding="utf-8")
    for d in ("@tailwind base", "@tailwind components", "@tailwind utilities"):
        assert d in directives, d
    pkg = PKG.read_text(encoding="utf-8")
    assert "tailwindcss" in pkg and "build:css" in pkg
    assert CONFIG.exists()


def test_cdn_removal_keeps_local_scripts_deferred(client):
    """Regressão do #101: scripts locais continuam deferidos."""
    html = client.get("/").get_data(as_text=True)
    for src in ("/static/scripts/index.js",):
        if src in html:
            m = re.search(r'<script[^>]*src="%s"[^>]*>' % re.escape(src), html)
            assert m and "defer" in m.group(0), src
