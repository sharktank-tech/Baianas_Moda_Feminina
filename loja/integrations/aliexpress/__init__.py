"""Integração AliExpress Open Platform (protocolo TOP router/rest).

Uso mínimo (Fase 1 - apenas client/auth HTTP):

    from loja.integrations.aliexpress import AliExpressClient, create_client_from_env

    client = create_client_from_env()            # lê ALIEXPRESS_APP_KEY/SECRET[/TOKEN]
    client = client.with_token(access_token)     # injeta token OAuth manualmente
    data = client.call("aliexpress.ds.text.search", keywords="...")  # Fase 2+

O transporte é injetável para testes (veja tests/test_aliexpress_client.py).
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

from .auth import AliExpressCredentials
from .client import AliExpressClient, DEFAULT_BASE_URL
from .errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from .models import AliExpressSearchProduct, AliExpressSearchResult
from .search import (
    build_search_params,
    normalize_search_response,
    parse_price,
)
from .sign import sign_top
from .transport import RequestsTransport, Transport, TransportError, TransportTimeout


def create_client_from_env(
    *,
    require_token: bool = False,
    base_url: Optional[str] = None,
    timeout: float = 10.0,
    transport: Optional[Transport] = None,
) -> AliExpressClient:
    """Constrói um AliExpressClient a partir de variáveis de ambiente.

    Variáveis: ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET (obrigatórias),
    ALIEXPRESS_ACCESS_TOKEN (opcional; exigida se require_token=True).
    Nunca embute secrets no código.
    """
    creds = AliExpressCredentials.from_env(require_token=require_token)
    resolved_base_url = base_url or os.environ.get("ALIEXPRESS_BASE_URL") or DEFAULT_BASE_URL
    return AliExpressClient(
        app_key=creds.app_key,
        app_secret=creds.app_secret,
        access_token=creds.access_token,
        base_url=resolved_base_url,
        timeout=timeout,
        transport=transport,
    )


__all__: Iterable[str] = [
    "AliExpressClient",
    "AliExpressCredentials",
    "AliExpressSearchResult",
    "AliExpressSearchProduct",
    "AliExpressError",
    "AliExpressConfigError",
    "AliExpressAuthError",
    "AliExpressAPIError",
    "AliExpressRateLimitError",
    "AliExpressTransportError",
    "sign_top",
    "build_search_params",
    "normalize_search_response",
    "parse_price",
    "create_client_from_env",
    "RequestsTransport",
    "Transport",
    "TransportError",
    "TransportTimeout",
]
