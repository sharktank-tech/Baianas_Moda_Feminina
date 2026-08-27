"""Exceções da integração AliExpress com redação obrigatória de secrets.

Nenhuma exceção deste módulo pode vazar app_secret, access_token/session,
refresh_token, sign ou DATABASE_URL em repr/log/str.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

_SECRET_KEYS = (
    "app_secret",
    "secret",
    "access_token",
    "session",
    "refresh_token",
    "sign",
    "password",
    "token",
    "authorization",
    "database_url",
    "database",
)

_MASK = "***REDACTED***"


def redact(value: Any) -> Any:
    """Retorna uma cópia de `value` com qualquer secret mascarado.

    Suporta dicts, listas/tuplas e strings. Strings que parecem tokens
    longos (>= 16 alfanuméricos) também são mascaradas por segurança.
    """
    if isinstance(value, Mapping):
        return {k: _redact_pair(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    if isinstance(value, str) and len(value) >= 16 and value.isalnum():
        return _MASK
    return value


def _redact_pair(key: str, val: Any) -> Any:
    if isinstance(key, str) and key.lower() in _SECRET_KEYS:
        return _MASK
    return redact(val)


class AliExpressError(Exception):
    """Base de todas as exceções da integração AliExpress."""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = redact(raw)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r})"


class AliExpressConfigError(AliExpressError):
    """Credenciais/parâmetros de configuração ausentes ou inválidos."""


class AliExpressAPIError(AliExpressError):
    """A API respondeu, mas com erro de negócio (code != 0 / error_response)."""

    def __init__(
        self,
        message: str,
        *,
        code: str | int | None = None,
        sub_code: str | int | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message, raw=raw)
        self.code = code
        self.sub_code = sub_code


class AliExpressAuthError(AliExpressAPIError):
    """Falha de autenticação/OAuth (token inválido, expirado, etc.)."""


class AliExpressRateLimitError(AliExpressAPIError):
    """Limite de requisições atingido. Nunca dispara retry infinito."""


class AliExpressTransportError(AliExpressError):
    """Falha de transporte (rede, timeout, HTTP != 200) antes de decodificar JSON."""


__all__: Iterable[str] = [
    "AliExpressError",
    "AliExpressConfigError",
    "AliExpressAuthError",
    "AliExpressAPIError",
    "AliExpressRateLimitError",
    "AliExpressTransportError",
    "redact",
]
