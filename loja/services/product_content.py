"""Renderização segura do conteúdo de produto (Fase 7.2 / 7.2.1).

A descrição de produtos AliExpress pode conter HTML, mas quase sempre vem de
fonte externa (AliExpress) e NUNCA deve ser confiada como segura.

Estratégia:
  * `sanitize_product_description_html()`  -> bleach: remove scripts, on*,
    javascript:, etc., mantendo apenas uma lista curta de tags permitidas.
    Para `<img src>`, aceita SOMENTE HTTPS e CDNs oficiais do AliExpress.
  * `product_description_excerpt()`        -> texto PREFERIDO para <meta> e
    cards da listagem: remove todo o HTML e corta em n palavras (sem HTML).

Os templates usam Jinja autoescape sobre o resultado de sanitize (sem |safe no
corpo da página). O extrato limpo (sem HTML) é usado no meta description e no
card, que já eram renderizados sem |safe.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

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

# CDNs oficiais de imagens do AliExpress. Aceitamos exatamente `host` ou
# qualquer subdomínio que termine em `.host` — nunca um hostname que apenas
# CONTÉM o texto (ex.: alicdn.com.evil.test é recusado).
_ALLOWED_IMG_HOSTS_SUFFIX = (".alicdn.com", ".aliexpress-media.com")
_ALLOWED_IMG_HOSTS = ("alicdn.com", "aliexpress-media.com")


def _is_allowed_img_host(hostname: str) -> bool:
    hostname = (hostname or "").strip().rstrip(".")
    if not hostname:
        return False
    lowered = hostname.lower()
    if lowered in _ALLOWED_IMG_HOSTS:
        return True
    return any(lowered.endswith(suffix) for suffix in _ALLOWED_IMG_HOSTS_SUFFIX)


# Atributos permitidos para <img> além de src (mesmo conjunto de antes).
_ALLOWED_IMG_NON_SRC_ATTRS = {"alt", "title", "width", "height"}


def _is_allowed_img_url(value: str) -> bool:
    """Valida um `<img src>` contra a política de segurança do sanitizer.

    Aceita apenas `src`:
      * scheme == https
      * hostname == alicdn.com / *.alicdn.com
      * hostname == aliexpress-media.com / *.aliexpress-media.com

    Rejeita http, //host/path (protocol-relative), data:, javascript:, hosts
    externos e falsos-positivos como `alicdn.com.evil.test`.
    """
    try:
        parsed = urlparse(value)
    except (ValueError, TypeError):
        return False

    # Garante URL absoluta https com host. `//host/path` fica sem scheme
    # (rejeitado), `data:`/`javascript:` usam outro scheme (rejeitado).
    if parsed.scheme != "https" or not parsed.netloc:
        return False

    return _is_allowed_img_host(parsed.hostname or "")


def _img_src_allowed(tag, name, value):
    """Callback de atributos do bleach para `<img>`.

    Quando passamos um callable para uma tag, o bleach DELEGA a decisão de TODOS
    os atributos da tag a esse callable (abandonando a allowlist). Por isso ele
    precisa permitir explicitamente apenas os atributos de img que estavam em
    `_ALLOWED_ATTRIBUTES` e, para `src`, aplicar a restrição de host.
    """
    if name == "src":
        return _is_allowed_img_url(value)

    # Para os demais atributos, restringe à allowlist original de img.
    return name in _ALLOWED_IMG_NON_SRC_ATTRS


# Ao usar um callable para `img`, o bleach chama esse callable para TODOS os
# atributos de <img> (não há allowlist separada para eles). O callable
# `_img_src_allowed` aplica a restrição de host ao `src` e devolve False para
# qualquer outro atributo que não esteja na allowlist de img (ex.: onerror).
_ALLOWED_ATTRIBUTES_WITH_IMG_SRC_CALLBACK = dict(_ALLOWED_ATTRIBUTES)
_ALLOWED_ATTRIBUTES_WITH_IMG_SRC_CALLBACK["img"] = _img_src_allowed

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
        attributes=_ALLOWED_ATTRIBUTES_WITH_IMG_SRC_CALLBACK,
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


class _ImageSrcExtractor(HTMLParser):
    """Extrai `<img src>` válidos da descrição, na ordem, sem duplicatas.

    Usa `html.parser.HTMLParser` da stdlib em vez de regex sobre HTML bruto.
    Aplica a MESMA política de segurança do sanitizer (`_is_allowed_img_url`):
    só HTTPS e CDNs oficiais do AliExpress.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        src = None
        for name, value in attrs:
            if name == "src":
                src = value
                break
        if src is None:
            return
        if not _is_allowed_img_url(src):
            return
        if src not in self._seen:
            self._seen.add(src)
            self.urls.append(src)


def product_description_image_urls(value: Optional[str]) -> list[str]:
    """Retorna os `<img src>` seguros presentes na descrição, em ordem e sem
    duplicatas.

    Aplica exatamente a política do sanitizer: somente HTTPS, somente
    alicdn.com / *.alicdn.com e aliexpress-media.com / *.aliexpress-media.com.
    Nunca retorna uma URL que o sanitizer recusaria.
    """
    if not value:
        return []
    extractor = _ImageSrcExtractor()
    extractor.feed(str(value))
    return extractor.urls


def sanitize_product_description_without_images(value: Optional[str]) -> Markup:
    """Sanitiza a descrição removendo TODAS as `<img>` (Fase 7.2.4).

    Usado na seção inferior "Descrição" dos produtos AliExpress, onde as
    imagens agora moram na galeria superior. Continua removendo script, style,
    iframe, on*, javascript: e URLs não autorizadas — apenas elimina `<img>`.
    """
    if not value:
        return Markup("")
    cleaned = bleach.clean(
        str(value),
        tags=_ALLOWED_TAGS - {"img"},
        attributes=_ALLOWED_ATTRIBUTES_WITH_IMG_SRC_CALLBACK,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return Markup(cleaned)


def _is_safe_gallery_url(value) -> bool:
    """Sanidade mínima para URLs canônicas/legadas da galeria (ProductImage e
    image1..image4).

    Aceita qualquer URL absoluta http(s) com host — nunca javascript:, data:
    nem relativa. Imagens extraídas da descrição já passaram pela política
    estrita de CDNs do AliExpress.
    """
    try:
        parsed = urlparse(str(value or ""))
    except (ValueError, TypeError):
        return False
    if not parsed.netloc:
        return False
    return parsed.scheme in ("http", "https")


def build_product_gallery(product) -> list[dict]:
    """Constrói a galeria canônica do produto no servidor (Fase 7.2.4).

    Ordem das fontes:
      1. ProductImage ordenado por position (fonte canônica importada)
      2. image1..image4 (compatibilidade legada, quando não há ProductImage)
      3. imagens seguras da description — SOMENTE para product.source ==
         "aliexpress"

    A lista final é deduplicada globalmente preservando a ordem. Se uma URL
    estiver em ProductImage e também na description, aparece UMA única vez.
    Não há limite artificial: 16 imagens válidas -> galeria com 16.

    Retorna uma lista de {"url", "alt"} (sem IDs externos/SKUs).
    """
    seen: set[str] = set()
    urls: list[str] = []

    def _maybe_add(url, *, strict: bool = False) -> None:
        url = (url or "").strip()
        if not url or url in seen:
            return
        if strict:
            ok = _is_allowed_img_url(url)
        else:
            ok = _is_safe_gallery_url(url)
        if not ok:
            return
        seen.add(url)
        urls.append(url)

    # 1. Fonte canônica importada.
    for image in sorted(product.images or [], key=lambda i: i.position or 0):
        _maybe_add(image.url)

    # 2. Compatibilidade legada.
    for legacy in (product.image1, product.image2, product.image3, product.image4):
        _maybe_add(legacy)

    # 3. Imagens da descrição (somente AliExpress).
    if getattr(product, "source", None) == "aliexpress":
        for url in product_description_image_urls(product.description):
            _maybe_add(url, strict=True)

    alt = product.name or "Imagem do produto"
    return [
        {"url": url, "alt": f"{alt} — imagem {i + 1}"}
        for i, url in enumerate(urls)
    ]


__all__ = [
    "sanitize_product_description_html",
    "sanitize_product_description_without_images",
    "product_description_excerpt",
    "product_description_image_urls",
    "build_product_gallery",
]
