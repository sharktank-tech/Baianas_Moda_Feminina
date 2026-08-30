"""Testes estruturais do middleware de callback OAuth da Vercel.

Sem runtime Node no ambiente, estes testes validam o contrato do
middleware.js na origem (matcher restrito, 302 sem query, atributos de
cookie, ausência de logging de valores sensíveis, fallback para o Flask).
Os placeholders usados nunca são valores reais de code/state.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIDDLEWARE = ROOT / "middleware.js"

MATCHER = "/integrations/aliexpress/callback"
COOKIE_PATH = "/integrations/aliexpress/callback"


def _src() -> str:
    return MIDDLEWARE.read_text(encoding="utf-8")


def test_middleware_exists_at_repo_root():
    assert MIDDLEWARE.is_file()


def test_matcher_restricted_to_oauth_callback():
    src = _src()
    assert 'matcher: ["/integrations/aliexpress/callback"]' in src
    # Nenhum outro caminho no matcher (sem "*", sem authorize, sem query).
    matcher_snippet = src.split("matcher: [")[1]
    assert "*" not in matcher_snippet.split("]")[0]
    assert "authorize" not in src


def test_redirect_is_302_without_query_string():
    src = _src()
    assert "Response.redirect" in src
    assert "302" in src
    # O destino é construído a partir do origin; a query original NÃO é copiada.
    assert "new URL(CALLBACK_PATH, url.origin)" in src
    redirect_snippet = src.split("Response.redirect")[1].split(")")[0]
    assert "?" not in redirect_snippet


def test_cookie_names_and_restricted_path():
    src = _src()
    assert "ae_oauth_code" in src
    assert "ae_oauth_state" in src
    assert f"Path={COOKIE_PATH}" in src


def test_cookie_attributes_present():
    src = _src()
    for attr in ["HttpOnly", "Secure", "SameSite=Lax", "Max-Age=", f"Path={COOKIE_PATH}"]:
        assert attr in src


def test_cookie_short_ttl():
    src = _src()
    assert re.search(r"COOKIE_AGE\s*=\s*120", src)


def test_callback_with_query_consumed_and_redirected():
    # Determina a tabela de decisão do middleware: COM code+state => 302;
    # SEM => passa adiante (undefined/null) para o Flask.
    src = _src()
    assert 'url.searchParams.has("code")' in src
    assert 'url.searchParams.has("state")' in src
    assert "return null;" in src
    assert "?? undefined" in src


def test_no_logging_of_url_code_or_state():
    src = _src()
    assert "console." not in src
    assert re.search(r"log\s*\(", src) is None


def test_default_middleware_export():
    src = _src()
    assert "export default function middleware" in src
    assert "buildCallbackResponse" in src