"""Prioridade 1 — headers de segurança globais (plano Weblyzer).

PR #99: os 5 headers seguros (nosniff, X-Frame-Options, Referrer-Policy,
Permissions-Policy, COOP). PR #100: Content-Security-Policy com allowlist
auditada contra os templates reais (Tailwind CDN, AdSense, Google Fonts,
ViaCEP). Valores vêm do próprio ``SECURITY_HEADERS`` para teste e módulo
nunca divergirem.
"""

from loja.security_headers import (
    CONTENT_SECURITY_POLICY,
    SECURITY_HEADERS,
)


def _csp(resp):
    """Parseia a política em {diretiva: [tokens]}."""
    policy = resp.headers["Content-Security-Policy"]
    out = {}
    for part in policy.split(";"):
        tokens = part.strip().split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


def test_home_response_includes_all_security_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    for header, value in SECURITY_HEADERS.items():
        assert resp.headers.get(header) == value, f"header {header}"


def test_security_headers_present_on_error_responses(client):
    resp = client.get("/pagina-que-nao-existe-pollo")
    assert resp.status_code == 404
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Cross-Origin-Opener-Policy") == "same-origin"


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


# ── PR #100: Content-Security-Policy ──────────────────────────────────────────


def test_csp_header_is_present_and_matches_module(client):
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy")
    assert csp == CONTENT_SECURITY_POLICY
    assert "default-src 'self'" in csp


def test_csp_default_src_is_self_without_scheme_wildcard(client):
    directives = _csp(client.get("/"))
    assert directives["default-src"] == ["'self'"]
    # Nenhum diretivo pode ter o curinga "https:" puro — exceto img-src,
    # que precisa de imagens de CDNs externos de produto (auditado).
    for directive, tokens in directives.items():
        if directive == "img-src":
            continue
        assert "https:" not in tokens, f"curinga https: em {directive}"


def test_csp_allows_google_fonts(client):
    directives = _csp(client.get("/"))
    assert "https://fonts.googleapis.com" in directives["style-src"]
    assert "https://fonts.gstatic.com" in directives["font-src"]


def test_csp_allows_tailwind_cdn_as_script_and_style(client):
    """O Play CDN é carregado como <script> — precisa estar em script-src."""
    directives = _csp(client.get("/"))
    assert "https://cdn.tailwindcss.com" in directives["script-src"]
    assert "https://cdn.tailwindcss.com" in directives["style-src"]


def test_csp_allows_viacep_in_connect_src(client):
    """Busca de CEP no front usa https://viacep.com.br."""
    directives = _csp(client.get("/"))
    assert "https://viacep.com.br" in directives["connect-src"]


def test_csp_allows_adsense_script_and_frames(client):
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
    assert resp.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
