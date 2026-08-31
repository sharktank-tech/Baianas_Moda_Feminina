"""Renderização segura do conteúdo de produto (Fase 7.2).

A descrição de produtos AliExpress pode conter HTML, mas quase sempre vem de
fonte externa (AliExpress) e NUNCA deve ser confiada como segura.

Estratégia:
  * `sanitize_product_description_html()`  -> bleach: remove scripts, on*,
    javascript:, etc., mantendo apenas uma lista curta de tags permitidas.
  * `product_description_excerpt()`        -> texto PREFERIDO para <meta> e
    cards da listagem: remove todo o HTML e corta em n palavras (sem HTML).

Os templates usam Jinja autoescape sobre o resultado de sanitize (sem |safe no
corpo da página). O extrato limpo (sem HTML) é usado no meta description e no
card, que já eram renderizados sem |safe.
"""

from __future__ import annotations

import re
from typing import Optional

import bleach
from markupsafe import Markup

# Tags mínimas e seguras para descrição de produto. Sem <script>, <style>,
# <iframe>, <form>, atributos on*, javascript: etc. Links são permitidos mas
# bleach restringe urls a http/https por padrão (e strip nonexistent).
_ALLOWED_TAGS = {
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "ul",
    "ol",
    "li",
    "span",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "blockquote",
    "code",
    "pre",
}

_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}

# bleach já adiciona rel="nofollow" para links. Reforçamos que target não
# quebra o sandbox: nunca abrimos javascript etc.
_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def sanitize_product_description_html(value: Optional[str]) -> Markup:
    """Sanitiza a descrição do produto para exibição segura.

    Retorna Markup para sinalizar ao template que o HTML já foi limpo, mas o
    template continua SEM usar |safe no corpo — usamos autoescape padrão do
    Jinja sobre o Markup retornado, garantindo que nada não-sanitizado escape.
    """
    if not value:
        return Markup("")
    cleaned = bleach.clean(
        str(value),
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return Markup(cleaned)


def product_description_excerpt(
    value: Optional[str],
    max_words: int = 40,
) -> str:
    """Extrato em texto puro (sem HTML) para <meta> e cards da listagem.

    Remove todas as tags e normaliza espaços; depois corta em `max_words`
    palavras. Nunca retorna HTML.
    """
    if not value:
        return ""
    text = _HTML_TAG_RE.sub(" ", str(value))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    words = text.split(" ")
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(".,;:!?") + "…"


__all__ = [
    "sanitize_product_description_html",
    "product_description_excerpt",
]
