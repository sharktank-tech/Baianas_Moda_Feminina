"""Fase 3 do plano Weblyzer — AdSense auto ads pós-load.

Mudança aprovada: o ``adsbygoogle.js`` sai do critical path (era o único
request de terceiro-party bloqueante do <head>) e passa a ser criado
dinamicamente no ``window load``.

Critério do plano: Weblyzer PageLoad ≤3.5s / requests ≤25.
"""

import re
from pathlib import Path

CLIENT = "ca-pub-1185235685562020"
SRC_RE = re.compile(r"""<script[^>]*src=["'][^"']*adsbygoogle\.js[^"']*["'][^>]*>""")


def test_home_has_no_eager_adsense_script_tag(client):
    """O adsbygoogle.js nao pode ser um <script src> estatico do head."""
    html = client.get("/").get_data(as_text=True)
    assert SRC_RE.search(html) is None, "adsbygoogle.js ainda carrega eagerly"


def test_adsense_loader_deferred_to_window_load(client):
    """Loader inline cria o script somente no evento window load."""
    html = client.get("/").get_data(as_text=True)
    assert "window.addEventListener('load'" in html
    assert "document.createElement('script')" in html
    assert f"adsbygoogle.js?client={CLIENT}" in html
    # o src esta DENTRO do loader (variavel), nao em atributo de tag
    assert SRC_RE.search(html) is None


def test_adsense_client_id_preserved():
    """Refactor nao pode perder o client id (causa silenciosa de zero renda)."""
    sources = [
        p.read_text(encoding="utf-8", errors="replace")
        for p in Path("loja/templates").rglob("*.html")
    ]
    hits = [s for s in sources if CLIENT in s]
    assert len(hits) == 1, f"client id em {len(hits)} templates (esperado: 1)"


def test_adsense_single_template_reference():
    """Apenas base.html referencia o AdSense (auto ads herda de todas as paginas)."""
    offenders = [
        str(p)
        for p in Path("loja/templates").rglob("*.html")
        if "adsbygoogle" in p.read_text(encoding="utf-8", errors="replace")
        and p.name != "base.html"
    ]
    assert offenders == [], offenders


def test_loader_stays_inline_pending_nonce_pr():
    """Hoje CSP tem 'unsafe-inline' — o loader e inline. Quando o PR do nonce
    adicionar o nonce neste <script>, este teste deve ser atualizado junto."""
    html = Path("loja/templates/base.html").read_text(encoding="utf-8")
    assert "window.addEventListener('load'" in html
