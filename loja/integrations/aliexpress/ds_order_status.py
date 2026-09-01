"""Adapter read-only DS de status de pedido AliExpress (Fase 8.0.7A) — DESABILITADO.

O PolloModas é DROPSHIPPER/BUYER: não usa seller-api nem
``aliexpress.solution.order.get/info.get``. O método DS adotado é:

    aliexpress.ds.trade.order.get   (business: order_id = AliExpress order id)

que responde (entre outros) ``order_status``, ``logistics_status``,
``logistics_info_list``, ``logistics_no`` e ``logistics_service``. Não existe
fallback runtime para ``aliexpress.trade.ds.order.get``: a implementação é
determinística em UM método.

ESTADO: este adapter NÃO é o default (``get_default_order_status_syncer()``
segue retornando ``HardOffOrderStatusSyncer``) — nenhum HTTP real é executado
em produção/dev a partir dele. Ele só roda quando construído explicitamente
(testes) ou acionado pelo probe manual com opt-in ``ALIEXPRESS_ORDER_STATUS_PROBE=1``.

Mapeamento remoto -> canônico (função pura ``map_aliexpress_ds_status``):
    * PLACE_ORDER_SUCCESS, PAYMENT_PROCESSING, RISK_CONTROL -> submitted;
    * WAIT_SELLER_SEND_GOODS (order ou logistics)           -> confirmed;
    * SELLER_SEND_PART_GOODS                                -> no máximo confirmed;
    * SELLER_SEND_GOODS (logistics) exempl.                 -> shipped;
    * WAIT_BUYER_ACCEPT_GOODS (order)                       -> shipped;
    * BUYER_ACCEPT_GOODS (logistics)                        -> delivered;
    * FUND_PROCESSING                                       -> NUNCA atua sozinho
      como primeira autoridade (só delivered se logistics == BUYER_ACCEPT_GOODS);
    * FINISH                                                -> conservador: nunca
      delivered/cancelled isoladamente; FINISH+BUYER_ACCEPT_GOODS -> delivered;
      FINISH+NO_LOGISTICS -> unknown;
    * IN_CANCEL                                             -> unknown (NUNCA
      cancelled: é cancelamento em andamento na AliExpress);
    * IN_ISSUE / IN_FROZEN / WAIT_SELLER_EXAMINE_MONEY / desconhecido -> unknown.

Quando order_status e logistics_status divergem, é usada a evidência mais
avançada SOMENTE quando semanticamente conclusiva (contida nas tabelas acima).
O adapter NUNCA produz ``cancelled`` (não existe cancelamento outbound).

SEGURANÇA:
    * token exclusivamente via credential_store -> request.supplier_id;
    * app_key/app_secret apenas da configuração (env), nunca do domínio;
    * config/infos e exceptions nunca contêm token, secret, session, headers,
      body, raw response nem payload bruto;
    * boundary ``AliExpressOrderStatusRequest`` jamais carrega token;
    * somente order_status/logistics_status (e request_id sanitizado) são
      extraídos; logistics_info_list só é tolerado, nunca persistido;
    * ``logistics_no``/``logistics_service`` são registrados apenas como
      entrada futura de tracking — NÃO criam Shipment/TrackingEvent aqui.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional, Set

from .client import AliExpressClient
from .credential_store import (
    CredentialExpired,
    CredentialInvalid,
    CredentialNotConfigured,
    CredentialNotFound,
    get_fulfillment_access_token,
)
from .errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from .status_sync import (
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_SHIPPED,
    STATUS_SUBMITTED,
    STATUS_UNKNOWN,
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusResult,
    AliExpressOrderStatusSyncError,
    AliExpressOrderStatusSyncer,
)

DS_ORDER_STATUS_METHOD = "aliexpress.ds.trade.order.get"
DEFAULT_TIMEOUT = 10.0

_RANK = {
    STATUS_SUBMITTED: 1,
    STATUS_CONFIRMED: 2,
    STATUS_SHIPPED: 3,
    STATUS_DELIVERED: 4,
}

#: order_status conclusivos (autoridade semântica própria).
_ORDER_CONCLUSIVE = {
    "PLACE_ORDER_SUCCESS": STATUS_SUBMITTED,
    "PAYMENT_PROCESSING": STATUS_SUBMITTED,
    "RISK_CONTROL": STATUS_SUBMITTED,
    "WAIT_SELLER_SEND_GOODS": STATUS_CONFIRMED,
    "SELLER_SEND_PART_GOODS": STATUS_CONFIRMED,
    "SELLER_SEND_GOODS": STATUS_SHIPPED,
    "WAIT_BUYER_ACCEPT_GOODS": STATUS_SHIPPED,
    "BUYER_ACCEPT_GOODS": STATUS_DELIVERED,
}

#: logistics_status conclusivos (ex.: SELLER_SEND_GOODS/BUYER_ACCEPT_GOODS).
_LOGISTICS_CONCLUSIVE = {
    "WAIT_SELLER_SEND_GOODS": STATUS_CONFIRMED,
    "SELLER_SEND_PART_GOODS": STATUS_CONFIRMED,
    "SELLER_SEND_GOODS": STATUS_SHIPPED,
    "BUYER_ACCEPT_GOODS": STATUS_DELIVERED,
}

#: order_status que sinalizam anormalidade/pendência: nunca viram avanço e
#: nunca cancelamento — só unknown (não inventamos delivered/cancelled).
_NEGATIVE_ORDER: Set[str] = {
    "IN_CANCEL",
    "IN_ISSUE",
    "IN_FROZEN",
    "WAIT_SELLER_EXAMINE_MONEY",
}

_ID_ALLOWED = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def map_aliexpress_ds_status(
    order_status: Any = None, logistics_status: Any = None
) -> str:
    """Função pura: remote (order_status + logistics_status) -> outcome canônico.

    Usa a evidência mais avançada apenas quando semanticamente conclusiva.
    Nunca retorna ``cancelled``: cancelamento em andamento vira unknown.
    """
    order_status = _clean(order_status).upper()
    logistics_status = _clean(logistics_status).upper()

    if order_status in _NEGATIVE_ORDER:
        return STATUS_UNKNOWN

    ordered = _ORDER_CONCLUSIVE.get(order_status)
    logistic = _LOGISTICS_CONCLUSIVE.get(logistics_status)

    candidates = [outcome for outcome in (ordered, logistic) if outcome]
    if not candidates:
        return STATUS_UNKNOWN
    return max(candidates, key=lambda outcome: _RANK[outcome])


def _sanitized_request_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not (0 < len(value) <= 64):
        return None
    if all(c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-" for c in value):
        return value
    return None


def _extract_statuses(payload: Any):
    """Extrai apenas order_status/logistics_status (e request_id sanitizado).

    Aceita envelope com wrapper ``result`` (real) ou campos diretos (testes).
    Levanta AliExpressOrderStatusSyncError sanitizado se inesperado/incompleto.
    """
    if not isinstance(payload, dict):
        raise AliExpressOrderStatusSyncError(
            "payload de status DS inesperado (não é objeto)"
        )
    data: Dict[str, Any] = payload
    for _ in range(2):
        inner = data.get("result")
        if isinstance(inner, dict):
            data = inner
            continue
        break

    order_status = _clean(data.get("order_status"))
    logistics_status = _clean(data.get("logistics_status"))
    request_id = _sanitized_request_id(data.get("request_id"))
    if request_id is None:
        request_id = _sanitized_request_id(payload.get("request_id"))

    if not (order_status or logistics_status):
        raise AliExpressOrderStatusSyncError(
            "payload de status DS sem order_status/logistics_status"
        )
    return order_status, logistics_status, request_id


class AliExpressDSOrderStatusSyncer(AliExpressOrderStatusSyncer):
    """Implementação ativa-only-on-demand de status via aliexpress.ds.trade.order.get.

    - identidade: ``request.ali_order_id`` (validada; inválida -> sem HTTP);
    - credencial: ``request.supplier_id`` -> credential_store (token em memória);
    - nunca usa ``attempt_key`` para consultar;
    - resultado: outcome canônico; exceptions de erro sanitizadas.
    """

    METHOD = DS_ORDER_STATUS_METHOD

    def __init__(
        self,
        *,
        client: Optional[Any] = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._timeout = timeout
        self.last_request_id: Optional[str] = None

    # ----- setup ---------------------------------------------------------
    def _resolve_token(self, supplier_id) -> str:
        try:
            return get_fulfillment_access_token(supplier_id)
        except CredentialNotConfigured:
            raise AliExpressOrderStatusSyncError(
                "credencial AliExpress não configurada para consulta DS"
            ) from None
        except CredentialNotFound:
            raise AliExpressOrderStatusSyncError(
                "credencial AliExpress não encontrada para consulta DS"
            ) from None
        except CredentialExpired:
            raise AliExpressOrderStatusSyncError(
                "credencial AliExpress expirada para consulta DS"
            ) from None
        except CredentialInvalid:
            raise AliExpressOrderStatusSyncError(
                "credencial AliExpress inválida para consulta DS"
            ) from None

    def _build_client(self, token: str) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            return self._client_factory(access_token=token)
        app_key = os.environ.get("ALIEXPRESS_APP_KEY")
        app_secret = os.environ.get("ALIEXPRESS_APP_SECRET")
        if not app_key or not app_secret:
            raise AliExpressOrderStatusSyncError(
                "ALIEXPRESS_APP_KEY/ALIEXPRESS_APP_SECRET ausentes para consulta DS"
            )
        return AliExpressClient(
            app_key=app_key,
            app_secret=app_secret,
            access_token=token,
            timeout=self._timeout,
        )

    # ----- interface -----------------------------------------------------
    def sync(
        self, request: AliExpressOrderStatusRequest
    ) -> AliExpressOrderStatusResult:
        if request is None:
            raise AliExpressOrderStatusSyncError("request nulo para consulta DS")

        if request.supplier_id is None:
            raise AliExpressOrderStatusSyncError(
                "supplier_id ausente para consulta DS"
            )

        order_id = _validated_ali_order_id(request.ali_order_id)
        token = self._resolve_token(request.supplier_id)
        client = self._build_client(token)

        try:
            payload = client.call(self.METHOD, order_id=order_id)
        except AliExpressAuthError as exc:
            raise AliExpressOrderStatusSyncError(
                f"erro de autenticação na consulta DS (code={exc.code})"
            ) from None
        except AliExpressRateLimitError as exc:
            raise AliExpressOrderStatusSyncError(
                f"rate limit na consulta DS (code={exc.code})"
            ) from None
        except AliExpressTransportError:
            raise AliExpressOrderStatusSyncError(
                "falha de transporte na consulta DS"
            ) from None
        except AliExpressAPIError as exc:
            raise AliExpressOrderStatusSyncError(
                f"erro de API na consulta DS (code={exc.code})"
            ) from None
        except Exception:
            # Nunca expor o texto da exceção (pode conter secrets do provider).
            raise AliExpressOrderStatusSyncError(
                "erro inesperado na consulta DS"
            ) from None

        order_status, logistics_status, request_id = _extract_statuses(payload)
        self.last_request_id = request_id
        outcome = map_aliexpress_ds_status(order_status, logistics_status)
        return AliExpressOrderStatusResult(
            outcome=outcome, external_status=outcome
        )


def _validated_ali_order_id(value: Any) -> str:
    """Valida o ID remoto antes de qualquer HTTP (sem vazar o valor em erros)."""
    text = _clean(value)
    if not text:
        raise AliExpressOrderStatusSyncError(
            "ali_order_id ausente para consulta DS"
        )
    if not (6 <= len(text) <= 64) or not all(c in _ID_ALLOWED for c in text):
        raise AliExpressOrderStatusSyncError(
            "ali_order_id em formato inválido para consulta DS"
        )
    return text


__all__ = [
    "DS_ORDER_STATUS_METHOD",
    "AliExpressDSOrderStatusSyncer",
    "map_aliexpress_ds_status",
]