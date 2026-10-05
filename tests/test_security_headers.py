"""Prioridade 1 — headers de segurança globais (plano Weblyzer).

PR #99: os 5 headers seguros (nosniff, X-Frame-Options, Referrer-Policy,
Permissions-Policy, COOP). PR #100: Content-Security-Policy com allowlist
auditada contra os templates reais.

PR P4: a CSP passa a ser montada por requisição com ``nonce``
(``csp_directives``) — ``script-src`` sem ``unsafe-inline``/``unsafe-eval``,
``script-src-attr 'none'`` e as allowlists mortas (Google Fonts, #104;
Tailwind CDN, #110) removidas. Os scripts inline da home carregam o mesmo
nonce do header, os templates não têm mais ``on*`` e os valores estáticos
vêm do próprio ``SECURITY_HEADERS`` para teste e módulo nunca divergirem.
"""

import re
from pathlib import Path

from loja.security_headers import (
    SECURITY_HEADERS,
    csp_directives,
)

TEMPLATES = Path("loja/templates")


def _csp(resp):
    """Parseia a política em {diretiva: [tokens]}."""
    policy = resp.headers["Content-Security-Policy"]
    out = {}
    for part in policy.split(";"):
        tokens = part.strip().split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


def _nonce(resp):
    m = re.search(
        r"'nonce-([0-9a-f]+)'", resp.headers["Content-Security-Policy"]
    )
    return m.group(1) if m else None


def test_home_response_includes_all_security_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    for header, value in SECURITY_HEADERS.items():
        assert resp.headers.get(header) == value, f"header {header}"
    assert "Content-Security-Policy" in resp.headers


def test_security_headers_present_on_error_responses(client):
    resp = client.get("/pagina-que-nao-existe-pollo")
    assert resp.status_code == 404
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert "Content-Security-Policy" in resp.headers


def test_password_reset_page_keeps_no_referrer_and_no_store(client):
    """2.9: a rota de redefinição define Referrer-Policy próprio; o global
    não pode sobrescrevê-lo (setdefault)."""
    resp = client.get("/redefinir-senha")
    assert resp.status_code == 200
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    # Os demais headers globais continuam valendo na mesma resposta.
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Permissions-Policy"] == SECURITY_HEADERS[
        "Permissions-Policy"
    ]


def test_security_headers_on_login_post_redirect(client, make_user):
    user = make_user()
    resp = client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    assert resp.status_code == 302
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == (
        "strict-origin-when-cross-origin"
    )


# ── CSP: por requisição, com nonce (PR P4) ───────────────────────────────────


def test_csp_header_is_present_and_matches_module(client):
    """O header é exatamente ``csp_directives(nonce)`` para o nonce sorteado."""
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy")
    assert "default-src 'self'" in csp
    nonce = _nonce(resp)
    assert nonce and len(nonce) == 64, "nonce hex de 32 bytes"
    assert csp == "; ".join(csp_directives(nonce))


def test_csp_nonce_is_unique_per_request(client):
    a, b = _nonce(client.get("/")), _nonce(client.get("/"))
    assert a and b and a != b


def test_csp_drops_unsafe_inline_and_eval(client):
    directives = _csp(client.get("/"))
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert directives["script-src-attr"] == ["'none'"]
    # P4b: style-src tambem perdeu unsafe-inline — blocos <style> usam
    # nonce e atributos style="" foram convertidos em classes.
    assert "'unsafe-inline'" not in directives["style-src"]
    assert directives["style-src-attr"] == ["'none'"]
    # o nonce de style-src e o mesmo do header (e dos blocos renderizados)
    assert any(t.startswith("'nonce-") for t in directives["style-src"])


def test_csp_default_src_is_self_without_scheme_wildcard(client):
    directives = _csp(client.get("/"))
    assert directives["default-src"] == ["'self'"]
    # Nenhum diretivo pode ter o curinga "https:" puro — exceto img-src,
    # que precisa de imagens de CDNs externos de produto (auditado).
    for directive, tokens in directives.items():
        if directive == "img-src":
            continue
        assert "https:" not in tokens, f"curinga https: em {directive}"


def test_csp_dropped_dead_google_fonts_allowlist(client):
    """#104 auto-hospedou as fontes — o allowlist deixa de existir."""
    directives = _csp(client.get("/"))
    assert "https://fonts.googleapis.com" not in directives["style-src"]
    assert "https://fonts.gstatic.com" not in directives["font-src"]
    assert "https://fonts.googleapis.com" not in directives["font-src"]


def test_csp_dropped_tailwind_cdn_allowlist(client):
    """#110 removeu o Play CDN — script-src e style-src perdem a origem."""
    directives = _csp(client.get("/"))
    assert "https://cdn.tailwindcss.com" not in directives["script-src"]
    assert "https://cdn.tailwindcss.com" not in directives["style-src"]


def test_csp_allows_viacep_in_connect_src(client):
    """Busca de CEP no front usa https://viacep.com.br."""
    directives = _csp(client.get("/"))
    assert "https://viacep.com.br" in directives["connect-src"]


def test_csp_allows_adsense_script_and_frames(client):
    """#111 moveu o loader para o window load; as origens seguem na
    allowlist (o <script> criado dinamicamente não recebe nonce)."""
    directives = _csp(client.get("/"))
    assert "https://*.googlesyndication.com" in directives["script-src"]
    assert "https://*.googlesyndication.com" in directives["frame-src"]
    assert "https://*.doubleclick.net" in directives["frame-src"]


def test_csp_hardening_directives(client):
    directives = _csp(client.get("/"))
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]
    assert directives["form-action"] == ["'self'"]
    assert directives["frame-ancestors"] == ["'self'"]
    assert directives["img-src"][:2] == ["'self'", "data:"]
    assert "upgrade-insecure-requests" in directives
    # block-all-mixed-content está deprecado; não deve voltar.
    assert "block-all-mixed-content" not in client.get("/").headers[
        "Content-Security-Policy"
    ]


def test_csp_present_on_password_reset_page(client):
    """setdefault: a rota 2.9 preserva no-referrer e ganha a CSP global."""
    resp = client.get("/redefinir-senha")
    assert resp.status_code == 200
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    csp = resp.headers["Content-Security-Policy"]
    nonce = _nonce(resp)
    assert nonce and csp == "; ".join(csp_directives(nonce))


def test_home_inline_scripts_carry_header_nonce(client):
    """Todo <script> sem src da home repassa o nonce exato do header."""
    resp = client.get("/")
    html = resp.get_data(as_text=True)
    nonce = _nonce(resp)
    srcless = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert srcless, "home deveria ter scripts inline"
    for tag in srcless:
        assert f'nonce="{nonce}"' in tag, tag


def test_error_page_inline_scripts_carry_nonce(client):
    """404 renderizado via template também precisa do nonce no script."""
    resp = client.get("/pagina-que-nao-existe-pollo")
    html = resp.get_data(as_text=True)
    srcless = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    nonce = _nonce(resp)
    for tag in srcless:
        assert f'nonce="{nonce}"' in tag, tag


def test_templates_have_no_inline_event_handlers():
    """25 handlers on* viraram data-attributes (script-src-attr 'none')."""
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if re.search(r"\son[a-z]+=", line):
                offenders.append(f"{path}:{lineno}")
    assert offenders == [], offenders


def test_templates_have_no_javascript_urls():
    offenders = [
        str(p)
        for p in TEMPLATES.rglob("*.html")
        if "javascript:" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], offenders


def test_templates_style_blocks_all_nonced():
    """P4b: todo <style> carrega o nonce (style-src sem unsafe-inline)."""
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            for m in re.finditer(r"<style[^>]*>", line):
                if 'nonce="{{ csp_nonce }}"' not in m.group(0):
                    offenders.append(f"{path}:{lineno}:{m.group(0)}")
    assert offenders == [], offenders


def test_templates_have_no_style_attributes():
    """P4b: os 9 atributos style="" viraram classes utilitárias."""
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if 'style="' in line or "style='" in line:
                offenders.append(f"{path}:{lineno}")
    assert offenders == [], offenders


def test_converted_utilities_are_in_built_css():
    """O tailwind.css versionado foi regerado com as classes das conversões."""
    css = Path("loja/static/styles/tailwind.css").read_text(encoding="utf-8")
    for probe in ("mt-7", "mt-2\\.5", "fef2f2", "ef4444", "991b1b", "1.15rem"):
        assert probe in css, probe
