"""Prioridade 1 — headers de segurança globais (plano Weblyzer).

Cobre os 5 headers seguros (sem CSP, que vem em PR próprio):
nosniff, X-Frame-Options, Referrer-Policy, Permissions-Policy e COOP.
Valores vêm do próprio ``SECURITY_HEADERS`` para o teste e o módulo
nunca divergirem.
"""

from loja.security_headers import SECURITY_HEADERS


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
