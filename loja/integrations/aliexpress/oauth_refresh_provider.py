"""Provider real de refresh OAuth AliExpress Open Platform (Fase 8.0.9).

Este módulo implementa SOMENTE o transporte/protocolo do refresh. A decisão de
habilitar HTTP real continua em ``oauth_refresh.get_default_oauth_refresher`` e
é fail-closed por feature flag.

Endpoint provado/documentado pela Open Platform:
    POST https://api-sg.aliexpress.com/rest/auth/token/refresh
    API path: /auth/token/refresh
    business: refresh_token=<token persistido>

O request usa o mesmo protocolo Open Platform já usado pelo exchange inicial:
``app_key``, ``timestamp``, ``sign_method=sha256`` e assinatura HMAC-SHA256
incluindo o API path no material. Não usa o signer DS ``/sync``.

Segurança:
    * app_secret e refresh_token nunca entram em logs/exceptions;
    * resposta bruta nunca é propagada;
    * erro explicitamente reconhecido como refresh token inválido/expirado vira
      ``AliExpressOAuthRefreshRejected``;
    * transporte/config/resposta inesperada vira ``...Unavailable``;
    * nenhuma chamada é feita no construtor — rede somente em ``refresh()``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping, Optional

from .errors import AliExpressAuthError
from .oauth import (
    DEFAULT_OAUTH_API_BASE_URL,
    OAuthTransport,
    RequestsOAuthTransport,
    normalize_oauth_token,
    sign_open_platform_request,
)
from .oauth_refresh import (
    AliExpressOAuthRefresher,
    AliExpressOAuthRefreshRejected,
    AliExpressOAuthRefreshRequest,
    AliExpressOAuthRefreshResult,
    AliExpressOAuthRefreshUnavailable,
)
from .transport import TransportError

OAUTH_REFRESH_API_PATH = "/auth/token/refresh"
DEFAULT_TIMEOUT = 10.0

# Somente códigos explicitamente ligados a refresh token inválido/expirado.
# Códigos desconhecidos permanecem indisponíveis, nunca são classificados por
# texto bruto do provider.
_REJECTED_CODES = frozenset(
    {
        "IllegalRefreshToken",
        "InvalidRefreshToken",
        "RefreshTokenExpired",
        "invalid_grant",
    }
)


def _timestamp_ms() -> str:
    return str(int(time.time() * 1000))


def _safe_code(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not (0 < len(text) <= 64):
        return None
    if all(c.isalnum() or c in "_.-" for c in text):
        return text
    return None


def _is_rejected(exc: AliExpressAuthError) -> bool:
    code = _safe_code(getattr(exc, "code", None))
    sub_code = _safe_code(getattr(exc, "sub_code", None))
    return code in _REJECTED_CODES or sub_code in _REJECTED_CODES


class AliExpressOpenPlatformOAuthRefresher(AliExpressOAuthRefresher):
    """Refresh real via Open Platform; construção é side-effect free."""

    def __init__(
        self,
        *,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        base_url: str = DEFAULT_OAUTH_API_BASE_URL,
        transport: Optional[OAuthTransport] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._base_url = base_url
        self._transport = transport
        self._timeout = timeout

    def _config(self) -> tuple[str, str]:
        app_key = self._app_key or os.environ.get("ALIEXPRESS_APP_KEY")
        app_secret = self._app_secret or os.environ.get("ALIEXPRESS_APP_SECRET")
        if not app_key or not app_secret:
            raise AliExpressOAuthRefreshUnavailable(
                "configuração OAuth ausente para refresh AliExpress"
            )
        return app_key, app_secret

    def refresh(
        self, request: AliExpressOAuthRefreshRequest
    ) -> AliExpressOAuthRefreshResult:
        if request is None or not getattr(request, "refresh_token", ""):
            raise AliExpressOAuthRefreshRejected(
                "refresh token ausente/inválido para renovação AliExpress"
            )

        app_key, app_secret = self._config()
        params: dict[str, str] = {
            "app_key": app_key,
            "refresh_token": request.refresh_token,
            "timestamp": _timestamp_ms(),
            "sign_method": "sha256",
        }
        params["sign"] = sign_open_platform_request(
            OAUTH_REFRESH_API_PATH, params, app_secret
        )
        url = self._base_url.rstrip("/") + OAUTH_REFRESH_API_PATH
        transport = self._transport or RequestsOAuthTransport(timeout=self._timeout)

        try:
            body = transport.post(url, params)
        except TransportError:
            raise AliExpressOAuthRefreshUnavailable(
                "falha de transporte no refresh AliExpress"
            ) from None
        except Exception:
            raise AliExpressOAuthRefreshUnavailable(
                "erro interno de transporte no refresh AliExpress"
            ) from None

        try:
            data: Mapping[str, Any] = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            raise AliExpressOAuthRefreshUnavailable(
                "resposta inválida no refresh AliExpress"
            ) from None

        try:
            token = normalize_oauth_token(data)
        except AliExpressAuthError as exc:
            if _is_rejected(exc):
                raise AliExpressOAuthRefreshRejected(
                    "refresh token rejeitado pela AliExpress"
                ) from None
            raise AliExpressOAuthRefreshUnavailable(
                "AliExpress não concluiu o refresh OAuth"
            ) from None
        except Exception:
            raise AliExpressOAuthRefreshUnavailable(
                "resposta inesperada no refresh AliExpress"
            ) from None

        if not token.access_token:
            raise AliExpressOAuthRefreshUnavailable(
                "refresh AliExpress sem access token"
            )

        return AliExpressOAuthRefreshResult(
            access_token=token.access_token,
            expires_in=token.expires_in,
            refresh_token=token.refresh_token,
            refresh_expires_in=token.refresh_expires_in,
        )


__all__ = [
    "OAUTH_REFRESH_API_PATH",
    "AliExpressOpenPlatformOAuthRefresher",
]
