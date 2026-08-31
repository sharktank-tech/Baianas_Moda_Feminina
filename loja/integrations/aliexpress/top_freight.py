"""Chamada isolada do frete do buyer via transporte TOP legado (eco.taobao.com).

Apenas o frete usa o router TOP (https://eco.taobao.com/router/rest) com
assinatura sign_top (md5). Search/product.get continuam no gateway /sync do
AliExpressClient existente, que NÃO é alterado.

Protocolo TOP (router/rest):
  - timestamp = YYYY-MM-DD HH:MM:SS em GMT+8
  - todos os parâmetros (públicos + negócio) via POST form-urlencoded
  - assinatura sign_top(params, app_secret, method="md5") já existente

Segurança: esta função nunca loga session/token/sign/app_secret/payload e
não reutiliza credenciais além do app_key/app_secret/access_token recebidos.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

import requests

from .buyer_freight import normalize_buyer_freight_response
from .errors import AliExpressAPIError
from .sign import sign_top
from .transport import TransportError, TransportTimeout

TOP_ROUTER_ENDPOINT = "https://eco.taobao.com/router/rest"
_TOP_TIMEOUT = 10.0
_GMT8 = timezone(timedelta(hours=8))


def top_gmt8_timestamp(now: Optional[datetime] = None) -> str:
    """Timestamp no formato TOP: YYYY-MM-DD HH:MM:SS em GMT+8."""
    current = now or datetime.now(timezone.utc)
    return current.astimezone(_GMT8).strftime("%Y-%m-%d %H:%M:%S")


def build_top_freight_calculate_dto(
    *,
    product_id: str,
    raw_sku_id: str,
    send_goods_country_code: str = "CN",
    country_code: str = "BR",
    product_num: int = 1,
) -> dict[str, Any]:
    """DTO mínimo para isolar somente o transporte (mesma semântica do /sync).

    `skuId` (camelCase) com o sku.id bruto; sem sku_id, province/city, price
    ou price_currency. O skuId permanece aqui porque o live /sync demonstrou
    que a implementação atual o exige; trocamos uma variável por vez.
    """
    try:
        product_id_num = int(product_id)
    except (TypeError, ValueError):
        raise ValueError("product_id deve ser numérico para o freight.calculate TOP")
    if not isinstance(product_num, int) or product_num < 1:
        raise ValueError("product_num deve ser um inteiro >= 1")
    return {
        "country_code": country_code,
        "product_id": product_id_num,
        "product_num": int(product_num),
        "send_goods_country_code": send_goods_country_code,
        "skuId": raw_sku_id,
    }


def build_top_freight_params(
    *,
    app_key: str,
    access_token: str,
    product_id: str,
    raw_sku_id: str,
    send_goods_country_code: str = "CN",
    country_code: str = "BR",
    product_num: int = 1,
    timestamp: Optional[str] = None,
) -> dict[str, str]:
    """Monta os parâmetros públicos do router TOP (sem `sign`, adicionado depois)."""
    dto = build_top_freight_calculate_dto(
        product_id=product_id,
        raw_sku_id=raw_sku_id,
        send_goods_country_code=send_goods_country_code,
        country_code=country_code,
        product_num=product_num,
    )
    return {
        "method": "aliexpress.logistics.buyer.freight.calculate",
        "app_key": app_key,
        "session": access_token,
        "timestamp": timestamp or top_gmt8_timestamp(),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "param_aeop_freight_calculate_for_buyer_d_t_o": json.dumps(
            dto, separators=(",", ":"), ensure_ascii=False
        ),
    }


def sign_top_freight_params(params: dict[str, str], app_secret: str) -> None:
    """Adiciona `sign` TOP (md5) in-place, via sign_top já existente."""
    params["sign"] = sign_top(params, app_secret, method="md5")


def _default_top_post(params: Mapping[str, str]) -> str:
    """POST form-urlencoded para o router TOP exclusivo do frete."""
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


def _raise_error_response(payload: Any) -> None:
    """Converte o wrapper de erro do TOP (error_response) em AliExpressAPIError."""
    if not isinstance(payload, Mapping) or "error_response" not in payload:
        return
    err = payload.get("error_response")
    if not isinstance(err, Mapping):
        raise AliExpressAPIError("erro TOP inesperado", raw=payload)
    msg = (
        err.get("sub_msg")
        or err.get("msg")
        or err.get("error_desc")
        or "erro do frete TOP"
    )
    raise AliExpressAPIError(
        str(msg),
        code=err.get("code") or err.get("error_code"),
        sub_code=err.get("sub_code"),
        raw=payload,
    )


def _is_zero_code(code: Any) -> bool:
    """Códigos zero-like (0, "0", "00", None) são sucesso (igual ao /sync)."""
    if code is None:
        return True
    value = str(code).strip()
    return bool(value) and value.isdigit() and int(value) == 0


def call_top_freight_calculate(
    *,
    app_key: str,
    app_secret: str,
    access_token: str,
    product_id: str,
    raw_sku_id: str,
    send_goods_country_code: str = "CN",
    country_code: str = "BR",
    product_num: int = 1,
    timestamp: Optional[str] = None,
    post: Optional[Callable[[Mapping[str, str]], str]] = None,
) -> Any:
    """Chamada única e isolada do freight.calculate via TOP (sem retries/sleep).

    Retorna AliExpressFreightResult normalizado. `post` é injetável nos testes
    (default: POST real form-urlencoded no router TOP). Desembrulha o wrapper
    do método (ex.: aliexpress_logistics_buyer_freight_calculate_response) da
    mesma forma que o cliente /sync.
    """
    params = build_top_freight_params(
        app_key=app_key,
        access_token=access_token,
        product_id=product_id,
        raw_sku_id=raw_sku_id,
        send_goods_country_code=send_goods_country_code,
        country_code=country_code,
        product_num=product_num,
        timestamp=timestamp,
    )
    sign_top_freight_params(params, app_secret)

    post_fn = post or _default_top_post
    body = post_fn(params)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise AliExpressAPIError(
            "resposta TOP inválida (não é JSON), host=eco.taobao.com"
        ) from exc

    _raise_error_response(data)
    if not isinstance(data, Mapping):
        raise AliExpressAPIError("resposta TOP inesperada (não é objeto)", raw=data)

    payload = data.get(
        "aliexpress_logistics_buyer_freight_calculate_response", data
    )
    if isinstance(payload, Mapping):
        code = payload.get("code")
        if code is not None and not _is_zero_code(code):
            msg = payload.get("msg") or "erro de API TOP"
            raise AliExpressAPIError(
                str(msg), code=code, sub_code=payload.get("sub_code"), raw=data
            )
    return normalize_buyer_freight_response(
        payload, method="aliexpress.logistics.buyer.freight.calculate"
    )


__all__: Iterable[str] = [
    "TOP_ROUTER_ENDPOINT",
    "top_gmt8_timestamp",
    "build_top_freight_calculate_dto",
    "build_top_freight_params",
    "sign_top_freight_params",
    "call_top_freight_calculate",
]