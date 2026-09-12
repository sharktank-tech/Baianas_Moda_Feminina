"""Placeorder AliExpress no gateway migrado ``/sync`` — FASE 8.0.11U/8.0.11W.

Este módulo substitui SOMENTE o transporte/protocolo do placeorder real. O DTO
canônico continua vindo do snapshot persistido e é construído pela função pura
``build_placeorder_dto`` já existente.

Contrato do gateway migrado usado pelo restante da integração PolloModas:

    endpoint       = https://api-sg.aliexpress.com/sync
    method         = aliexpress.trade.buy.placeorder
    business param = param_place_order_request4_open_api_d_t_o
    sign_method    = sha256
    sign           = HMAC-SHA256 sobre todos os params exceto ``sign``
    timestamp      = Unix epoch em milissegundos

Garantias:
- zero retry / zero sleep;
- transporte injetável nos testes;
- nenhum log de app_key, token/session, app_secret, sign, payload ou remote ids;
- erros de negócio são normalizados para ``TopPlaceOrderResponse`` para preservar
  a classificação conservadora já existente no provider;
- estruturas ambíguas continuam ``AliExpressAPIError`` sanitizado;
- ``is_success=false`` acompanhado de qualquer evidência de identidade remota é
  contraditório e falha fechado como protocolo ambíguo, nunca como rejeição segura.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional

from .errors import AliExpressAPIError
from .sign import sign_sync
from .top_placeorder import TopPlaceOrderResponse, build_placeorder_dto, parse_top_placeorder_response
from .transport import RequestsTransport


SYNC_PLACEORDER_ENDPOINT = "https://api-sg.aliexpress.com/sync"
SYNC_PLACEORDER_METHOD = "aliexpress.trade.buy.placeorder"
SYNC_PLACEORDER_BUSINESS_PARAM = "param_place_order_request4_open_api_d_t_o"
_SYNC_TIMEOUT = 10.0


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def build_sync_placeorder_params(
    *,
    app_key: str,
    access_token: str,
    request,
    timestamp: Optional[str] = None,
) -> dict[str, str]:
    dto = build_placeorder_dto(request)
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

    # Erro TOP/Open Platform canônico: preserva para o parser existente.
    if "error_response" in data:
        return data

    wrapper_key = SYNC_PLACEORDER_METHOD.replace(".", "_") + "_response"
    dotted_key = f"{SYNC_PLACEORDER_METHOD}_response"

    wrapped = data.get(wrapper_key, data.get(dotted_key))
    if isinstance(wrapped, Mapping):
        # O parser histórico espera o wrapper ``aliexpress_trade_buy_placeorder_response``.
        return {"aliexpress_trade_buy_placeorder_response": wrapped}

    # ``simplify=true`` pode devolver diretamente result/is_success em alguns
    # métodos. Normalizamos explicitamente sem aceitar shapes arbitrários.
    if isinstance(data.get("result"), Mapping):
        return {"aliexpress_trade_buy_placeorder_response": data}

    raise AliExpressAPIError("resposta placeorder inesperada")


def _has_remote_identity_evidence(raw_order_list: Any) -> bool:
    """Detecta presença de identidade remota sem materializar/expor IDs.

    Em resposta de falha, qualquer elemento não vazio — inclusive shape
    inesperado — é suficiente para considerar o protocolo contraditório e manter
    o outcome como desconhecido. Estruturas explicitamente vazias não contam.
    """
    if raw_order_list is None:
        return False
    if isinstance(raw_order_list, list):
        return bool(raw_order_list)
    if isinstance(raw_order_list, Mapping):
        numbers = raw_order_list.get("number")
        if numbers is None:
            return False
        if isinstance(numbers, list):
            return bool(numbers)
        return True
    return True


def _reject_contradictory_failure_identity(normalized: Mapping[str, Any]) -> None:
    wrapper = normalized.get("aliexpress_trade_buy_placeorder_response")
    if not isinstance(wrapper, Mapping):
        return
    result = wrapper.get("result")
    if not isinstance(result, Mapping):
        return
    if result.get("is_success") is not False:
        return
    if _has_remote_identity_evidence(result.get("order_list")):
        raise AliExpressAPIError("resposta placeorder contraditoria")


def parse_sync_placeorder_response(payload: Any) -> TopPlaceOrderResponse:
    normalized = _extract_protocol_payload(payload)
    _reject_contradictory_failure_identity(normalized)
    return parse_top_placeorder_response(normalized)


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
