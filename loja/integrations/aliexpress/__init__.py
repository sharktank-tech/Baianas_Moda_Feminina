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
from .credential_store import (
    AliExpressCredential,
    is_configured,
    persist_credential,
    get_fulfillment_access_token,
    mark_credential_error,
    CredentialStoreError,
    CredentialNotConfigured,
    CredentialInvalid,
    CredentialNotFound,
    CredentialExpired,
)
from .fulfillment import (
    ensure_aliexpress_fulfillment,
    cancel_fulfillment_before_submission,
    FulfillmentResult,
    ERROR_CODES,
)
from .submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionFailed,
    AliExpressSubmissionItem,
    AliExpressSubmissionRequest,
    AliExpressPlaceOrderItem,
    AliExpressSubmissionResult,
    AliExpressSubmissionUnknown,
    HardOffOrderSubmitter,
    SubmissionHardOffError,
    RemoteOrderPersistenceAmbiguous,
    submit_aliexpress_order,
    get_default_order_submitter,
)
from loja.modules.models import (
    AliExpressSubmissionAttempt,
    AliExpressRemoteOrder,
)
from .reconciliation import (
    AliExpressOrderReconciler,
    AliExpressOrderReconciliationRequest,
    AliExpressOrderReconciliationResult,
    AliExpressReconciliationError,
    AliExpressReconciliationUnavailable,
    HardOffOrderReconciler,
    get_default_order_reconciler,
    reconcile_aliexpress_order,
    RECONCILE_FOUND,
    RECONCILE_NOT_FOUND,
    RECONCILE_UNKNOWN,
)
from .status_sync import (
    AliExpressOrderStatusSyncer,
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusResult,
    AliExpressOrderStatusSyncError,
    AliExpressOrderStatusSyncUnavailable,
    HardOffOrderStatusSyncer,
    get_default_order_status_syncer,
    sync_aliexpress_order_status,
    STATUS_SUBMITTED,
    STATUS_CONFIRMED,
    STATUS_SHIPPED,
    STATUS_DELIVERED,
    STATUS_CANCELLED,
    STATUS_UNKNOWN,
)
from .ds_order_status import (
    DS_ORDER_STATUS_METHOD,
    AliExpressDSOrderStatusSyncer,
    map_aliexpress_ds_status,
)
from .oauth_refresh import (
    AliExpressOAuthRefreshError,
    AliExpressOAuthRefresher,
    AliExpressOAuthRefreshRejected,
    AliExpressOAuthRefreshRequest,
    AliExpressOAuthRefreshResult,
    AliExpressOAuthRefreshUnavailable,
    AliExpressCredentialRefreshResult,
    HardOffOAuthRefresher,
    get_default_oauth_refresher,
    refresh_aliexpress_credential,
    evaluate_credential_with_refresh,
)
from .top_freight import (
    TOP_ROUTER_ENDPOINT,
    build_top_freight_calculate_dto,
    build_top_freight_params,
    call_top_freight_calculate,
    sign_top_freight_params,
    top_gmt8_timestamp,
)
from .transport import RequestsTransport, Transport, TransportError, TransportTimeout

# Registra o listener fail-closed que congela CPF válido nos snapshots novos.
# Import deliberadamente tardio evita interferir nos imports dos models/boundaries.
from . import snapshot_checkout_tax_id as _snapshot_checkout_tax_id  # noqa: F401,E402


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
    "AliExpressCredential",
    "is_configured",
    "persist_credential",
    "get_fulfillment_access_token",
    "mark_credential_error",
    "CredentialStoreError",
    "CredentialNotConfigured",
    "CredentialInvalid",
    "CredentialNotFound",
    "CredentialExpired",
    "ensure_aliexpress_fulfillment",
    "cancel_fulfillment_before_submission",
    "FulfillmentResult",
    "ERROR_CODES",
    "AliExpressOrderSubmitter",
    "AliExpressSubmissionFailed",
    "AliExpressSubmissionItem",
    "AliExpressSubmissionRequest",
    "AliExpressSubmissionResult",
    "AliExpressSubmissionUnknown",
    "HardOffOrderSubmitter",
    "SubmissionHardOffError",
    "RemoteOrderPersistenceAmbiguous",
    "AliExpressSubmissionAttempt",
    "AliExpressRemoteOrder",
    "submit_aliexpress_order",
    "get_default_order_submitter",
    "AliExpressOrderReconciler",
    "AliExpressOrderReconciliationRequest",
    "AliExpressOrderReconciliationResult",
    "AliExpressReconciliationError",
    "AliExpressReconciliationUnavailable",
    "HardOffOrderReconciler",
    "get_default_order_reconciler",
    "reconcile_aliexpress_order",
    "RECONCILE_FOUND",
    "RECONCILE_NOT_FOUND",
    "RECONCILE_UNKNOWN",
    "AliExpressOrderStatusSyncer",
    "AliExpressOrderStatusRequest",
    "AliExpressOrderStatusResult",
    "AliExpressOrderStatusSyncError",
    "AliExpressOrderStatusSyncUnavailable",
    "HardOffOrderStatusSyncer",
    "get_default_order_status_syncer",
    "sync_aliexpress_order_status",
    "STATUS_SUBMITTED",
    "STATUS_CONFIRMED",
    "STATUS_SHIPPED",
    "STATUS_DELIVERED",
    "STATUS_CANCELLED",
    "STATUS_UNKNOWN",
    "AliExpressOAuthRefreshError",
    "AliExpressOAuthRefresher",
    "AliExpressOAuthRefreshRejected",
    "AliExpressOAuthRefreshRequest",
    "AliExpressOAuthRefreshResult",
    "AliExpressOAuthRefreshUnavailable",
    "AliExpressCredentialRefreshResult",
    "HardOffOAuthRefresher",
    "get_default_oauth_refresher",
    "refresh_aliexpress_credential",
    "evaluate_credential_with_refresh",
    "DS_ORDER_STATUS_METHOD",
    "AliExpressDSOrderStatusSyncer",
    "map_aliexpress_ds_status",
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
