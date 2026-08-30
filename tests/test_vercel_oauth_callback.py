"""Testes estruturais do endpoint Node de sanitização do callback OAuth.

Sem runtime Node no ambiente, validamos na origem (api/...) e no vercel.json o
contrato de roteamento: o callback exato vai para a função Node (antes do
catch-all Python), sem logs e sem vazamento de query para o Flask. Os
placeholders nunca são valores reais de code/state.
"""

from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE_FN = ROOT / "api" / "aliexpress-oauth-callback.js"
VERCEL = ROOT / "vercel.json"


def _js() -> str:
    return NODE_FN.read_text(encoding="utf-8")


def _vercel() -> dict:
    return json.loads(VERCEL.read_text(encoding="utf-8"))


def test_node_function_exists():
    assert NODE_FN.is_file()


def test_node_function_has_no_logging():
    js = _js()
    # Nenhuma chamada real de logging (comentário explicativo é permitido).
    for call in [
        "console.log(",
        "console.error(",
        "console.info(",
        "console.warn(",
        "console.debug(",
    ]:
        assert call not in js
    assert re.search(r"\bprint\s*\(", js) is None


def test_node_only_accepts_get():
    js = _js()
    assert 'req.method !== "GET"' in js
    assert "405" in js


def test_node_rejects_without_code_and_state():
    js = _js()
    assert "if (!code || !state)" in js
    assert "400" in js
    assert '"Invalid OAuth callback"' in js
    assert "no-store" in js


def test_node_redirects_302_without_query():
    js = _js()
    assert "302" in js
    assert 'res.setHeader("Location", COMPLETE_PATH)' in js
    # O destino é CONSTANTE: nunca derivado da query do request.
    assert "COMPLETE_PATH" in js
    assert "/integrations/aliexpress/callback/complete" in js
    location_snippet = js.split("COMPLETE_PATH")[2]
    assert "?" not in location_snippet


def test_node_sets_http_only_cookies_with_attributes():
    js = _js()
    assert "ae_oauth_code" in js
    assert "ae_oauth_state" in js
    for attr in [
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
        "Max-Age=",
        "Path=${COOKIE_PATH}",
    ]:
        assert attr in js
    assert 'const COOKIE_PATH = "/integrations/aliexpress/callback"' in js
    assert "const MAX_AGE = 120" in js
    assert "encodeURIComponent" in js


def test_vercel_has_node_builder_for_callback():
    v = _vercel()
    assert v["version"] == 2
    builds = v["builds"]
    assert builds[0] == {
        "src": "api/aliexpress-oauth-callback.js",
        "use": "@vercel/node",
    }
    assert builds[1]["src"] == "run.py"
    assert builds[1]["use"] == "@vercel/python"
    assert builds[1]["config"]["maxDuration"] == 60


def test_vercel_routes_callback_node_before_python_catchall():
    v = _vercel()
    routes = v["routes"]
    # Ordem obrigatória: callback exato -> Node; catch-all Python depois.
    assert routes[0]["src"] == "^/integrations/aliexpress/callback$"
    assert routes[0]["dest"] == "/api/aliexpress-oauth-callback.js"
    assert routes[1]["src"] == "/(.*)"
    assert routes[1]["dest"] == "run.py"


def test_vercel_node_route_is_exact_not_prefix():
    # Rota exata (âncora ^$): /callback/complete NÃO é interceptada pelo Node.
    v = _vercel()
    assert "^" in v["routes"][0]["src"]
    assert "$" in v["routes"][0]["src"]