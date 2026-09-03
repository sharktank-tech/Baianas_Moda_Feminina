"""Protocolo TOP do placeorder AliExpress (router/rest) — camada pura de transporte.

Este módulo NÃO toca banco nem catálogo. Recebe um
``AliExpressSubmissionRequest`` canônico (já congelado a partir do submission
snapshot) e produz:

  * ``build_placeorder_dto``  -> DTO de negócio determinístico;
  * ``build_top_placeorder_params`` -> parâmetros públicos TOP (sem sign);
  * ``sign_top_placeorder_params`` -> assinatura md5 (reusa ``sign_top``);
  * ``call_top_placeorder`` -> UMA chamada lógica ao router (post injetável);
  * ``parse_top_placeorder_response`` -> resultado 0/1/N remote order ids.

Contrato oficial (resource ali trade.buy.placeorder / router/rest):

  method         = aliexpress.trade.buy.placeorder
  endpoint       = https://eco.taobao.com/router/rest
  business param = param_place_order_request4_open_api_d_t_o
  sign_method    = md5  (reusa sign_top de .sign)

Divisão de responsabilidades (Fase 8.0.11A):

  * O PARSER decide apenas se a resposta é uma RESPOSTA PROTOCOLAR válida
    (wrapper reconhecido / error_response bem formado). Respostas de negócio,
    inclusive ``is_success=false`` e ``error_response``, são NORMALIZADAS para
    ``TopPlaceOrderResponse`` — nunca levantadas como exceção aqui.
  * O PROVIDER decide a REGRA DE NEGÓCIO: confirmed failure vs unknown
    (timeout/desconexão/ambiguidade), de forma conservadora.

Levantar ``AliExpressAPIError`` aqui é reservado EXCLUSIVAMENTE para estrutura
que NÃO representa uma resposta protocolar válida (JSON quebrado, wrapper
ausente, shape structuralmente impossível). Nesses casos usa-se só um erro
sanitizado — nunca raw/payload/body.

Segurança: nada aqui loga/expoe app_key/app_secret/session/sign/payload/
endereço/remote order ids. Erros de transporte carregam apenas metadados
sanitizados (host, path, http_status, content_type, response_length).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional

import requests

from .errors import AliExpressAPIError, AliExpressConfigError
from .sign import sign_top
from .transport import TransportError, TransportTimeout

if TYPE_CHECKING:  # pragma: no cover - apenas para type checker
    from .submission import AliExpressSubmissionRequest


TOP_ROUTER_ENDPOINT = "https://eco.taobao.com/router/rest"
_TOP_TIMEOUT = 10.0
_GMT8 = timezone(timedelta(hours=8))

_METHOD = "aliexpress.trade.buy.placeorder"
_WRAPPER = "aliexpress_trade_buy_placeorder_response"
_BUSINESS_PARAM = "param_place_order_request4_open_api_d_t_o"

# Campos de endereço oficialmente aceitos pelo DTO. Ausentes/indefinidos são
# OMITIDOS — não se inventa cpf/tax_number/full_name/passport/address2 etc.
_SHIPPING_FIELDS = (
    "address",
    "city",
    "contact_person",
    "country",
    "mobile_no",
    "province",
    "zip",
)


@dataclass(frozen=True)
class TopPlaceOrderResponse:
    """Resposta normalizada do placeorder.

    ``order_ids`` e ``error_message`` (texto remoto) NUNCA aparecem em repr.
    ``error_code``/``error_sub_code`` podem aparecer (são códigos autenticados,
    não PII/secrets).
    """

    is_success: bool
    order_ids: tuple[str, ...] = field(default_factory=tuple, repr=False)
    error_code: Optional[str] = None
    error_sub_code: Optional[str] = None
    error_message: Optional[str] = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "<TopPlaceOrderResponse "
            f"is_success={self.is_success} "
            f"order_count={len(self.order_ids)} "
            f"error_code={self.error_code!r} "
            f"error_sub_code={self.error_sub_code!r}>"
        )


def top_gmt8_timestamp(now: Optional[datetime] = None) -> str:
    """Timestamp TOP: YYYY-MM-DD HH:MM:SS em GMT+8."""
    current = now or datetime.now(timezone.utc)
    return current.astimezone(_GMT8).strftime("%Y-%m-%d %H:%M:%S")


def _coerce_product_id(value: Any) -> int:
    """Converte product_id numérico para int, bloqueando não numéricos/bool."""
    if isinstance(value, bool):
        raise AliExpressConfigError("product_id inválido (bool)")
    if isinstance(value, int):
        if value <= 0:
            raise AliExpressConfigError("product_id inválido (<= 0)")
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or not cleaned.isdigit():
            raise AliExpressConfigError("product_id inválido")
        as_int = int(cleaned)
        if as_int <= 0:
            raise AliExpressConfigError("product_id inválido (<= 0)")
        return as_int
    raise AliExpressConfigError("product_id inválido")


def _coerce_product_count(value: Any) -> int:
    """Exige INTEGER real >= 1. Rejeita float/string/bool/fração."""
    if isinstance(value, bool):
        raise AliExpressConfigError("product_count inválido (bool)")
    if not isinstance(value, int):
        raise AliExpressConfigError(
            "product_count deve ser inteiro real (não float/string)"
        )
    if value < 1:
        raise AliExpressConfigError("product_count inválido (< 1)")
    return value


def build_placeorder_dto(request: "AliExpressSubmissionRequest") -> dict[str, Any]:
    """Constrói o DTO canônico SOMENTE a partir ``request.shipping_address`` e
    ``request.items``.

    Nenhum acesso a Order/Product/ProductVariant/AliExpressOrderItem/
    SupplierProduct. Campos de endereço ausentes são omitidos; ``cpf``,
    ``tax_number``, ``full_name``, ``passport`` etc. NÃO são inventados.

    Item:
      product_count  int >= 1        (product_count preservado como int)
      product_id     int > 0         (convertido de string numérica válida)
      sku_attr       str  congelada
      logistics_service_name  somente quando presente/non-empty
    """
    shipping = {
        key: str(request.shipping_address[key])
        for key in _SHIPPING_FIELDS
        if str(request.shipping_address.get(key) or "").strip()
    }

    items = []
    for entry in request.items:
        item = {
            "product_count": _coerce_product_count(entry.product_count),
            "product_id": _coerce_product_id(entry.product_id),
            "sku_attr": str(entry.sku_attr or "").strip(),
        }
        if not item["sku_attr"]:
            raise AliExpressConfigError("sku_attr obrigatório (não pode ser vazio)")
        if getattr(entry, "logistics_service_name", None):
            item["logistics_service_name"] = str(entry.logistics_service_name).strip()

        items.append(item)

    return {
        "logistics_address": shipping,
        "product_items": items,
    }


def build_top_placeorder_params(
    *,
    app_key: str,
    access_token: str,
    request: "AliExpressSubmissionRequest",
    timestamp: Optional[str] = None,
) -> dict[str, str]:
    """Parâmetros públicos TOP (sem `sign`, adicionado depois via sign_top).

    O DTO de negócio é serializado de forma determinística:
    ``json.dumps(dto, separators=(",", ":"), ensure_ascii=False)``.
    """
    dto = build_placeorder_dto(request)
    return {
        "method": _METHOD,
        "app_key": app_key,
        "session": access_token,
        "timestamp": timestamp or top_gmt8_timestamp(),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        _BUSINESS_PARAM: json.dumps(dto, separators=(",", ":"), ensure_ascii=False),
    }


def sign_top_placeorder_params(params: dict[str, str], app_secret: str) -> None:
    """Assina os parâmetros TOP in-place via sign_top (md5) já existente."""
    params["sign"] = sign_top(params, app_secret, method="md5")


def _default_top_post(params: Mapping[str, str]) -> str:
    """POST form-urlencoded real ao router TOP (default NÃO alcançável pela
    aplicação em produção nesta fase — o provider fica HARD-OFF)."""
    try:
        resp = requests.post(
            TOP_ROUTER_ENDPOINT,
            data=dict(params),
            timeout=_TOP_TIMEOUT,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
    except requests.Timeout as exc:
        raise TransportTimeout(f"timeout após {_TOP_TIMEOUT}s") from exc
    except requests.RequestException as exc:
        raise TransportError(
            f"falha de rede: {type(exc).__name__} host=eco.taobao.com path=/router/rest"
        ) from exc

    content_type = resp.headers.get("Content-Type", "")
    length = len(resp.content)
    if resp.status_code != 200:
        raise TransportError(
            f"HTTP {resp.status_code} host=eco.taobao.com path=/router/rest "
            f"content_type={content_type} response_length={length}"
        )
    _body = resp.text or ""
    _looks_json = "json" in content_type.lower() or _body.lstrip().startswith("{")
    if not _looks_json:
        raise TransportError(
            "HTTP 200 inesperado (não-JSON) host=eco.taobao.com path=/router/rest "
            f"response_length={length}"
        )
    return _body


def _is_valid_order_id(value: Any) -> bool:
    """ID remoto válido: int/str decimal POSITIVO. bool/0/negativo/vazio inválido."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        cleaned = value.strip()
        return bool(cleaned) and cleaned.isdigit() and int(cleaned) > 0
    return False


def _extract_order_list(result: Any) -> tuple[str, ...]:
    """Normaliza order_list preservando TODOS os ids, validando cada elemento.

    Aceita ``[123, 456]``, ``{"number": [123, 456]}``, ``{"number": 123}``,
    ``[]`` e ``{"number": []}``.

    Se um elemento for estruturalmente inválido, NÃO é descartado nem retornado
    parcialmente — o protocolo é considerado inválido (``AliExpressAPIError``
    sanitizado). Nunca ``str(None)``, nunca filtrar ids ruins mantendo bons.
    """
    if not isinstance(result, Mapping):
        return ()
    raw = result.get("order_list")
    if raw is None:
        return ()

    if isinstance(raw, list):
        elements = raw
    elif isinstance(raw, Mapping):
        numbers = raw.get("number", [])
        if numbers is None:
            return ()
        elements = numbers if isinstance(numbers, list) else [numbers]
    else:
        raise AliExpressAPIError("resposta placeorder inesperada")

    # bool também é int/subclasse; checar explicitamente em _is_valid_order_id.
    out = []
    for element in elements:
        if not _is_valid_order_id(element):
            raise AliExpressAPIError("resposta placeorder inesperada")
        out.append(str(int(element) if not isinstance(element, str) else element))
    return tuple(out)


def parse_top_placeorder_response(payload: Any) -> "TopPlaceOrderResponse":
    """Faz o parse do wrapper oficial, preservando 0/1/N remote order ids.

    Respostas de negócio — inclusive ``is_success=False`` e ``error_response``
    bem formado — são NORMALIZADAS (sem exceção). A exceção é reservada apenas
    para estrutura que não representa resposta protocolar válida.

    Retorna ``TopPlaceOrderResponse``. ``error_message`` guarda o texto remoto
    somente EM MEMÓRIA (nunca em repr).
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError("resposta placeorder inesperada")

    if "error_response" in payload:
        err = payload["error_response"]
        if not isinstance(err, Mapping):
            raise AliExpressAPIError("resposta placeorder inesperada")
        code = str(err.get("code") or "").strip()
        sub_code = str(err.get("sub_code") or "").strip()
        msg = (
            str(err.get("sub_msg") or err.get("msg") or "").strip()
            or None
        )
        return TopPlaceOrderResponse(
            is_success=False,
            order_ids=(),
            error_code=code or None,
            error_sub_code=sub_code or None,
            error_message=msg,
        )

    wrapper = payload.get(_WRAPPER)
    if not isinstance(wrapper, Mapping):
        raise AliExpressAPIError("resposta placeorder inesperada")

    result = wrapper.get("result")
    if not isinstance(result, Mapping):
        raise AliExpressAPIError("resposta placeorder inesperada")

    raw_is_success = result.get("is_success")
    if not isinstance(raw_is_success, bool):
        raise AliExpressAPIError("resposta placeorder inesperada: is_success deve ser bool")
    is_success = raw_is_success
    # Do DTO oficial, falha pode vir por error_code/error_msg quando is_success
    # é false; normaliza para o provider classificar (nunca exception).
    error_code = str(result.get("error_code") or "").strip() or None
    error_msg = str(result.get("error_msg") or "").strip() or None

    order_ids = _extract_order_list(result)

    if is_success:
        return TopPlaceOrderResponse(
            is_success=True,
            order_ids=order_ids,
            error_code=None,
            error_sub_code=None,
            error_message=None,
        )

    return TopPlaceOrderResponse(
        is_success=False,
        order_ids=(),
        error_code=error_code,
        error_sub_code=None,
        error_message=error_msg,
    )


def call_top_placeorder(
    *,
    app_key: str,
    app_secret: str,
    access_token: str,
    request: "AliExpressSubmissionRequest",
    timestamp: Optional[str] = None,
    post: Optional[Callable[[Mapping[str, str]], str]] = None,
) -> "TopPlaceOrderResponse":
    """Executa UMA chamada lógica ao router TOP (sem retry, sem sleep).

    build → sign → POST único → json.loads → parse → TopPlaceOrderResponse.

    ``error_response`` NÃO é transformado em exception antes do parser; o
    provider decide a regra de negócio. Exceptions aqui ficam restritas a:
      * TransportTimeout / TransportError (rede);
      * AliExpressAPIError (apenas JSON/estrutura inválida — erro sanitizado).
    """
    params = build_top_placeorder_params(
        app_key=app_key,
        access_token=access_token,
        request=request,
        timestamp=timestamp,
    )
    sign_top_placeorder_params(params, app_secret)

    post_fn = post or _default_top_post
    body = post_fn(params)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise AliExpressAPIError(
            "resposta placeorder inválida"
        ) from exc

    return parse_top_placeorder_response(data)


__all__: Iterable[str] = [
    "TOP_ROUTER_ENDPOINT",
    "top_gmt8_timestamp",
    "build_placeorder_dto",
    "build_top_placeorder_params",
    "sign_top_placeorder_params",
    "call_top_placeorder",
    "parse_top_placeorder_response",
    "TopPlaceOrderResponse",
]
