"""Validação compartilhada de URLs do rodapé (links do admin).

Regra única usada na renderização (omitir registros antigos inválidos sem
apagar dados) e nos formulários do admin (impedir novos placeholders):

- aceita caminhos internos que resolvem em uma rota funcional da aplicação
  (ex.: /sobre, /produtos, /politica-privacidade, /acompanhar-pedido);
- aceita URLs externas https:// com host (ex.: perfis de redes sociais);
- rejeita valor vazio, "#", javascript:, data: (e outros esquemas ativos),
  URLs http:// sem TLS, caminhos que não resolvem e qualquer destino inválido.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from werkzeug.exceptions import MethodNotAllowed
from werkzeug.routing import RequestRedirect

_BLOCKED_PREFIXES = ("javascript:", "data:", "vbscript:", "file:")


def _has_bad_chars(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 32 for ch in value)


def _is_external_https(value: str) -> bool:
    if not value.startswith("https://"):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    host = parts.hostname or ""
    if not host or "." not in host:
        return False
    # Sem userinfo embutido (user@host) nem controles.
    if "@" in (parts.netloc or ""):
        return False
    return True


def _resolves_internally(path: str) -> bool:
    """Confere se o caminho interno corresponde a uma rota funcional."""
    from flask import current_app

    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return False
    clean = path.split("?", 1)[0].split("#", 1)[0]
    if not clean or _has_bad_chars(clean):
        return False
    try:
        adapter = current_app.url_map.bind("")
    except RuntimeError:
        return False
    try:
        adapter.match(clean)
    except RequestRedirect:
        # Rota existe; só exige barra final (ex.: /acompanhar-pedido/).
        return True
    except MethodNotAllowed:
        # Rota existe (método diferente de GET).
        return True
    except Exception:
        return False
    return True


def is_safe_footer_url(url: str | None) -> bool:
    """Retorna True apenas para destinos seguros do rodapé."""
    if not url or not isinstance(url, str):
        return False
    value = url.strip()
    if not value or value == "#" or _has_bad_chars(value):
        return False
    lowered = value.lower()
    if lowered.startswith(_BLOCKED_PREFIXES):
        return False
    if lowered.startswith("http://"):
        return False
    if value.startswith("https://"):
        return _is_external_https(value)
    if value.startswith("/"):
        return _resolves_internally(value)
    return False
