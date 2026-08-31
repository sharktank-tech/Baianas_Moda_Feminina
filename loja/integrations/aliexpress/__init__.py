"""Integração AliExpress Open Platform (gateway migrado `/sync`).

Uso mínimo:

    from loja.integrations.aliexpress import AliExpressClient, create_client_from_env

    client = create_client_from_env()            # lê ALIEXPRESS_APP_KEY/SECRET[/TOKEN]
    client = client.with_token(access_token)     # injeta token OAuth manualmente
    data = client.call("aliexpress.ds.text.search", keyWord="...")  # Fase 2+

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
from .models import (
    AliExpressSearchProduct,
    AliExpressSearchResult,
    AliExpressProductDetail,
    AliExpressProductSku,
    AliExpressFreightResult,
    AliExpressFreightOption,
)
from .product import (
    build_product_detail_params,
    normalize_product_detail,
)
from .search import (
    build_search_params,
    normalize_search_response,
    parse_price,
)
from .freight import (
    build_freight_params,
    normalize_freight_response,
)
from .buyer_freight import (
    build_buyer_freight_params,
    normalize_buyer_freight_response,
)
from .buyer_freight_get import (
    build_buyer_freight_get_params,
    canonical_freight_sku_id,
    freight_product_id,
    freight_product_sku_shape,
    normalize_buyer_freight_get_response,
    raw_canonical_sku_id,
)
from .sign import sign_sync, sign_top
from .top_freight import (
    TOP_ROUTER_ENDPOINT,
    build_top_freight_calculate_dto,
    build_top_freight_params,
    call_top_freight_calculate,
    sign_top_freight_params,
    top_gmt8_timestamp,
)
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
    "AliExpressProductDetail",
    "AliExpressProductSku",
    "AliExpressFreightResult",
    "AliExpressFreightOption",
    "AliExpressError",
    "AliExpressConfigError",
    "AliExpressAuthError",
    "AliExpressAPIError",
    "AliExpressRateLimitError",
    "AliExpressTransportError",
    "sign_top",
    "sign_sync",
    "build_search_params",
    "normalize_search_response",
    "parse_price",
    "build_product_detail_params",
    "normalize_product_detail",
    "build_freight_params",
    "normalize_freight_response",
    "build_buyer_freight_params",
    "normalize_buyer_freight_response",
    "build_buyer_freight_get_params",
    "canonical_freight_sku_id",
    "freight_product_id",
    "freight_product_sku_shape",
    "raw_canonical_sku_id",
    "normalize_buyer_freight_get_response",
    "create_client_from_env",
    "TOP_ROUTER_ENDPOINT",
    "top_gmt8_timestamp",
    "build_top_freight_calculate_dto",
    "build_top_freight_params",
    "sign_top_freight_params",
    "call_top_freight_calculate",
    "RequestsTransport",
    "Transport",
    "TransportError",
    "TransportTimeout",
]
