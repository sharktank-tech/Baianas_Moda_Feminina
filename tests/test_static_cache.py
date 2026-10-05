"""P6 — Cache de borda para /static (plano Weblyzer — Performance).

Critério: estabilizar FCP/DCL — hoje cada asset invoca a Lambda
(``Cache-Control: no-cache`` + ``X-Vercel-Cache: MISS``).
"""

from loja.static_cache import STATIC_CACHE_CONTROL


def test_static_asset_gets_public_cache_control(client):
    resp = client.get("/static/styles/tailwind.css")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == STATIC_CACHE_CONTROL
    assert "max-age=60" in resp.headers["Cache-Control"]
    assert "s-maxage=3600" in resp.headers["Cache-Control"]
    # ETag continua vindo (revalidação do navegador após 60s)
    assert resp.headers.get("ETag")


def test_static_js_and_font_also_cached(client):
    for path in (
        "/static/scripts/index.js",
        "/static/styles/fonts.css",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["Cache-Control"] == STATIC_CACHE_CONTROL, path


def test_html_is_not_cached_by_this_installer(client):
    """Só /static/* recebe a política — HTML/API intactos."""
    resp = client.get("/")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "s-maxage" not in cc
    assert "max-age=60" not in cc


def test_static_keeps_security_headers(client):
    """Coexistência: after_request de segurança continua valendo no asset."""
    resp = client.get("/static/styles/tailwind.css")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in resp.headers


def test_static_has_no_vary_cookie(client):
    """'Vary: Cookie' (marcado pela sessão do Flask) impede o cache da
    borda da Vercel — em /static ele precisa sair."""
    resp = client.get("/static/styles/tailwind.css")
    vary = resp.headers.get("Vary", "")
    assert "Cookie" not in vary, vary
