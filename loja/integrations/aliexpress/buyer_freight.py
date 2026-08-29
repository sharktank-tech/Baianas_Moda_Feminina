"""Frete oficial do buyer (Fase 4b, read-only, sem persistência).

Implementa aliexpress.logistics.buyer.freight.calculate. Primeiro testamos se o
gateway atual /sync (sign_method=sha256, session=token) aceita esse método —
a documentação antiga ainda aponta o TOP legado com hmac/md5. Se o Open Platform
responder "method not found"/"permission denied"/etc., o erro é preservado e
sanitizado pela camada de transporte (client.call -> _parse_response).
"""

import json
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional

from .errors import AliExpressAPIError
from .models import AliExpressFreightOption, AliExpressFreightResult
from .search import _as_int, _as_str, parse_price


def build_buyer_freight_params(
    *,
    product_id: str,
    country_code: str = "BR",
    product_num: int = 1,
    send_goods_country_code: str = "CN",
    province_code: Optional[str] = None,
    city_code: Optional[str] = None,
    price: Optional[Decimal] = None,
    price_currency: Optional[str] = None,
) -> Mapping[str, str]:
    """Monta os parâmetros de negócio (business) para buyer.freight.calculate.

    `param_aeop_freight_calculate_for_buyer_d_t_o` recebe um JSON compacto e
    participa da assinatura gerada em client.py. province_code/city_code e
    price/price_currency são opcionais. No primeiro smoke price/price_currency
    são omitidos.
    """
    if not product_id:
        raise ValueError("product_id é obrigatório para buyer.freight.calculate")
    if not country_code:
        raise ValueError("country_code é obrigatório para buyer.freight.calculate")
    if not send_goods_country_code:
        raise ValueError(
            "send_goods_country_code é obrigatório para buyer.freight.calculate"
        )
    if not isinstance(product_num, int) or product_num < 1:
        raise ValueError("product_num deve ser um inteiro >= 1")

    dto: dict = {
        "country_code": country_code,
        "product_id": product_id,
        "product_num": product_num,
        "send_goods_country_code": send_goods_country_code,
        "province_code": province_code or "",
        "city_code": city_code or "",
    }
    if price is not None and price_currency is not None:
        dto["price"] = str(price)
        dto["price_currency"] = price_currency

    business_param = json.dumps(dto, separators=(",", ":"))
    return {"param_aeop_freight_calculate_for_buyer_d_t_o": business_param}


def _extract_buyer_options(value: Any) -> List[Mapping[str, Any]]:
    """Navega pelos possíveis wrappers até a lista de opções de frete."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, Mapping)]
    if isinstance(value, Mapping):
        for key in (
            "aeop_freight_calculate_result_for_buyer_d_t_o_list",
            "result",
            "data",
        ):
            item = value.get(key)
            if isinstance(item, (list, Mapping)):
                found = _extract_buyer_options(item)
                if found:
                    return found
        if any(k in value for k in ("service_name", "freight", "error_code")):
            return [value]
    return []


def _normalize_buyer_option(option: Mapping[str, Any]) -> AliExpressFreightOption:
    freight = option.get("freight")
    if isinstance(freight, Mapping):
        amount = freight.get("amount")
        cent = freight.get("cent")
        currency = freight.get("currency_code")
    else:
        amount = option.get("freight_amount")
        cent = option.get("freight_cent")
        currency = option.get("currency_code")
    # freight.amount é o valor monetário principal; só cai em cent se ausente.
    if amount is None:
        amount = cent
    shipping_fee = parse_price(amount) if amount is not None else None
    if currency is None:
        currency = option.get("currency_code")

    return AliExpressFreightOption(
        service_name=_as_str(option.get("service_name")),
        shipping_fee=shipping_fee,
        currency=_as_str(currency),
        delivery_days=_as_int(option.get("estimated_delivery_time")),
        estimated_delivery=_as_str(option.get("estimated_delivery_time")),
        raw=dict(option),
    )


def normalize_buyer_freight_response(
    payload: Any, *, method: str
) -> AliExpressFreightResult:
    """Normaliza a resposta oficial para AliExpressFreightResult.

    Wrapper esperado: aliexpress_logistics_buyer_freight_calculate_response ->
    result[...list]. Lança AliExpressAPIError se `payload` não for objeto.
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError(
            "resposta de buyer.freight.calculate inesperada (não é objeto)",
            raw=payload,
        )

    options_raw = _extract_buyer_options(payload)
    options = [_normalize_buyer_option(o) for o in options_raw]

    currency: Optional[str] = None
    for o in options:
        if o.currency:
            currency = o.currency
            break

    fees = [o.shipping_fee for o in options if o.shipping_fee is not None]
    days = [o.delivery_days for o in options if o.delivery_days is not None]
    min_fee = min(fees) if fees else None
    min_days = min(days) if days else None

    return AliExpressFreightResult(
        options=options,
        currency=currency,
        min_fee=min_fee,
        min_delivery_days=min_days,
        raw=dict(payload),
    )


__all__: Iterable[str] = [
    "build_buyer_freight_params",
    "normalize_buyer_freight_response",
]
