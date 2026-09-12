"""Placeorder AliExpress no gateway migrado ``/sync`` — FASE 8.0.11U/8.0.11X.

A 8.0.11X preserva no DTO apenas campos opcionais de endereço já congelados no
snapshot. Nada é reconstruído do catálogo e nenhum valor é inventado.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional

from .errors import AliExpressAPIError
from .sign import sign_sync
from .top_placeorder import (
    TopPlaceOrderResponse,
    build_placeorder_dto,
    parse_top_placeorder_response,
)
from .transport import RequestsTransport


SYNC_PLACEORDER_ENDPOINT = "https://api-sg.aliexpress.com/sync"
SYNC_PLACEORDER_METHOD = "aliexpress.trade.buy.placeorder"
SYNC_PLACEORDER_BUSINESS_PARAM = "param_place_order_request4_open_api_d_t_o"
_SYNC_TIMEOUT = 10.0

# Campos opcionais aceitos pelo contrato oficial e suportados pelo snapshot
# canônico desta fase. CPF/tax_number/location ids NÃO são adicionados aqui.
_OPTIONAL_SHIPPING_FIELDS = (
    "address2",
    "full_name",
    "phone_country",
)


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _build_sync_dto(request) -> dict[str, Any]:
    dto = build_placeorder_dto(request)
    shipping = dto.get("logistics_address")
    if not isinstance(shipping, dict):
        raise AliExpressAPIError("resposta placeorder inesperada")

    source = getattr(request, "shipping_address", None)
    if not isinstance(source, Mapping):
        return dto

    for field in _OPTIONAL_SHIPPING_FIELDS:
        value = str(source.get(field) or "").strip()
        if value:
            shipping[field] = value
    return dto


def build_sync_placeorder_params(
    *,
    app_key: str,
    access_token: str,
    request,
    timestamp: Optional[str] = None,
) -> dict[str, str]:
    dto = _build_sync_dto(request)
    return {
        "method": SYNC_PLACEORDER_METHOD,
        "app_key": app_key,
        "session": access_token,
        "timestamp": timestamp or _now_ms(),
        "format": "json",
        "v": "2.0",
        "simplify": "true",
        "sign_method": "sha256",
        SYNC_PLACEORDER_BUSINESS_PARAM: json.dumps(
            dto, separators=(",", ":"), ensure_ascii=False
        ),
    }


def sign_sync_placeorder_params(params: dict[str, str], app_secret: str) -> None:
    params["sign"] = sign_sync(params, app_secret)


def _extract_protocol_payload(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise AliExpressAPIError("resposta placeorder inesperada")

    if "error_response" in data:
        return data

    wrapper_key = SYNC_PLACEORDER_METHOD.replace(".", "_") + "_response"
    dotted_key = f"{SYNC_PLACEORDER_METHOD}_response"

    wrapped = data.get(wrapper_key, data.get(dotted_key))
    if isinstance(wrapped, Mapping):
        return {"aliexpress_trade_buy_placeorder_response": wrapped}

    if isinstance(data.get("result"), Mapping):
        return {"aliexpress_trade_buy_placeorder_response": data}

    raise AliExpressAPIError("resposta placeorder inesperada")


def parse_sync_placeorder_response(payload: Any) -> TopPlaceOrderResponse:
    return parse_top_placeorder_response(_extract_protocol_payload(payload))


def call_sync_placeorder(
    *,
    app_key: str,
    app_secret: str,
    access_token: str,
    request,
    timestamp: Optional[str] = None,
    post: Optional[Callable[[dict[str, str]], str]] = None,
) -> TopPlaceOrderResponse:
    """Executa exatamente UMA chamada lógica ao gateway ``/sync``."""
    params = build_sync_placeorder_params(
        app_key=app_key,
        access_token=access_token,
        request=request,
        timestamp=timestamp,
    )
    sign_sync_placeorder_params(params, app_secret)

    sender = post
    if sender is None:
        sender = RequestsTransport(SYNC_PLACEORDER_ENDPOINT, timeout=_SYNC_TIMEOUT).post

    raw = sender(dict(params))
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AliExpressAPIError("resposta placeorder inesperada") from exc

    return parse_sync_placeorder_response(data)


__all__ = [
    "SYNC_PLACEORDER_ENDPOINT",
    "SYNC_PLACEORDER_METHOD",
    "SYNC_PLACEORDER_BUSINESS_PARAM",
    "build_sync_placeorder_params",
    "sign_sync_placeorder_params",
    "parse_sync_placeorder_response",
    "call_sync_placeorder",
]
