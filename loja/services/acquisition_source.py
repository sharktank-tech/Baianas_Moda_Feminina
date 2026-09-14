"""FASE 9.0.0 — captura sanitizada de origem de tráfego.

Somente leitura de metadados de navegação: nunca inspeciona, loga ou persiste
payload bruto da URL. UTM/referrer são normalizados com allowlist estrita de
caracteres; valores ausentes ou inválidos permanecem ``None`` (fail-closed).

Atribuição first-touch: a primeira origem válida vista na sessão é mantida até
o pedido ser criado (e nunca é reescrita por páginas internas seguintes).
"""

from __future__ import annotations

from urllib.parse import urlsplit
from typing import Optional

_ACQUISITION_MAXLEN = {
    "source": 64,
    "medium": 64,
    "campaign": 120,
    "referrer": 120,
}

# Allowlist estrita: letras, dígitos, '_', '.', '-', espaço.
_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._- "
)

_SESSION_KEY = "acquisition"


def _clean(value, max_length: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch in _ALLOWED_CHARS)
    cleaned = cleaned.strip(" ._-")
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned or None


def _own_host(request_like) -> Optional[str]:
    host = getattr(request_like, "host", None)
    if not host:
        return None
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _base_host(hostname: str) -> str:
    hostname = hostname.lower().strip()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def sanitize_utm(
    request_like,
) -> dict:
    """Extrai utm_source/medium/campaign do request com allowlist estrita."""
    args = getattr(request_like, "args", {}) or {}
    return {
        "source": _clean(args.get("utm_source"), _ACQUISITION_MAXLEN["source"]),
        "medium": _clean(args.get("utm_medium"), _ACQUISITION_MAXLEN["medium"]),
        "campaign": _clean(
            args.get("utm_campaign"), _ACQUISITION_MAXLEN["campaign"]
        ),
    }


def sanitize_referrer(request_like) -> Optional[str]:
    """Hostname externo do referrer (primeira página), sem path/query/credenciais."""
    referrer = getattr(request_like, "referrer", None) or ""
    if not referrer.strip():
        return None
    try:
        parts = urlsplit(referrer)
    except (ValueError, TypeError):
        return None
    host = _base_host(parts.hostname or "")
    if not host:
        return None
    own = _own_host(request_like)
    if own and host == own:
        return None
    return _clean(host, _ACQUISITION_MAXLEN["referrer"])


def capture_acquisition(request_like, *, session=None, force_referrer=True) -> dict:
    """First-touch: persiste a primeira origem válida na sessão.

    Chama o callback ``session`` como dict-like (Flask session). Nunca realiza
    I/O e nunca falha: exceções internas são convertidas em origem vazia.
    Sempre devolve as 4 chaves (source/medium/campaign/referrer).
    """
    try:
        captured = {}
        utm = sanitize_utm(request_like)
        if any(utm.values()):
            captured.update(utm)

        if force_referrer or not captured:
            referrer = sanitize_referrer(request_like)
            if referrer:
                captured["referrer"] = referrer

        if not captured or session is None:
            return {
                "source": captured.get("source"),
                "medium": captured.get("medium"),
                "campaign": captured.get("campaign"),
                "referrer": captured.get("referrer"),
            }

        stored = session.get(_SESSION_KEY) or {}
        if stored:
            return {key: stored.get(key) for key in _ACQUISITION_MAXLEN}

        session[_SESSION_KEY] = captured
        return {key: captured.get(key) for key in _ACQUISITION_MAXLEN}
    except Exception:
        return {
            "source": None,
            "medium": None,
            "campaign": None,
            "referrer": None,
        }


def order_acquisition(*, session=None) -> dict:
    """Campos de origem já capturados (first-touch) para persistir no Order."""
    if session is None:
        return {}
    stored = dict(session.get(_SESSION_KEY) or {})
    return {
        "source": stored.get("source"),
        "medium": stored.get("medium"),
        "campaign": stored.get("campaign"),
        "referrer": stored.get("referrer"),
    }


def acquisition_to_order_kwargs(acquisition: dict) -> dict:
    """Traduz o dict do serviço para argumentos do modelo Order (acquisition_*)."""
    return {
        "acquisition_source": acquisition.get("source"),
        "acquisition_medium": acquisition.get("medium"),
        "acquisition_campaign": acquisition.get("campaign"),
        "acquisition_referrer": acquisition.get("referrer"),
    }


__all__ = [
    "capture_acquisition",
    "order_acquisition",
    "acquisition_to_order_kwargs",
    "sanitize_referrer",
    "sanitize_utm",
]