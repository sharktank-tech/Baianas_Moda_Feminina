"""Frete oficial do buyer via aliexpress.logistics.buyer.freight.get (read-only).

Fase 4b (isolada): depois de confirmar que aliexpress.logistics.buyer.freight.
calculate exigia skuId, testamos agora aliexpress.logistics.buyer.freight.get.
Atenção: o parâmetro externo é `aeopFreightCalculateForBuyerDTO` (D de DTO,
sem underscores), NÃO `param_aeop_freight_calculate_for_buyer_d_t_o`.

O SDK contemporâneo usa exatamente esta combinação:

    method    = aliexpress.logistics.buyer.freight.get
    parameter = aeopFreightCalculateForBuyerDTO

Mantemos gateway /sync, sign_method=sha256 e access_token como `session`.
"""

import json
import logging
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional

from .errors import AliExpressAPIError
from .models import AliExpressFreightOption, AliExpressFreightResult
from .search import _as_int, _as_str, parse_price

logger = logging.getLogger(__name__)


def build_buyer_freight_get_params(
    *,
    product_id: str,
    sku_id: Optional[str] = None,
    country_code: str = "BR",
    product_num: int = 1,
    send_goods_country_code: str = "CN",
    province_code: Optional[str] = None,
    city_code: Optional[str] = None,
) -> Mapping[str, str]:
    """Monta o business param para aliexpress.logistics.buyer.freight.get.

    O DTO JSON usa tipos corretos (product_id/product_num como number) e
    province_code/city_code só quando preenchidos. NÃO envia sku_id (o live do
    freight.get não exige e aceita melhor sem). O parâmetro externo é
    `aeopFreightCalculateForBuyerDTO` (JSON compacto que participa da
    assinatura), NÃO o `param_...d_t_o` do freight.calculate.
    """
    if not product_id:
        raise ValueError("product_id é obrigatório para buyer.freight.get")
    if not country_code:
        raise ValueError("country_code é obrigatório para buyer.freight.get")
    if not send_goods_country_code:
        raise ValueError(
            "send_goods_country_code é obrigatório para buyer.freight.get"
        )
    try:
        product_id_num = int(product_id)
    except (TypeError, ValueError):
        raise ValueError("product_id deve ser numérico para buyer.freight.get")
    if not isinstance(product_num, int) or product_num < 1:
        raise ValueError("product_num deve ser um inteiro >= 1")

    dto: dict = {
        "product_id": product_id_num,
        "product_num": int(product_num),
        "country_code": country_code,
        "send_goods_country_code": send_goods_country_code,
    }
    if province_code:
        dto["province_code"] = province_code
    if city_code:
        dto["city_code"] = city_code

    return {"aeopFreightCalculateForBuyerDTO": json.dumps(dto, separators=(",", ":"))}


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "1", "y", "yes")


def _extract_get_options(value: Any) -> List[Mapping[str, Any]]:
    """Localiza a lista de opções de frete em aliexpress...freight.get.

    Wrapper esperado:

        aliexpress_logistics_buyer_freight_get_response
        └── result
            └── aeop_freight_calculate_result_for_buyer_dtolist
                └── aeop_freight_calculate_result_for_buyer_d_t_o

    Também aceita lista direta no wrapper intermediário (dtolist como lista de
    opções) e opção única (dict) diretamente no payload.
    """
    if isinstance(value, list):
        out: List[Mapping[str, Any]] = []
        for v in value:
            if not isinstance(v, Mapping):
                continue
            inner = v.get("aeop_freight_calculate_result_for_buyer_d_t_o")
            if isinstance(inner, Mapping):
                out.append(inner)
            elif isinstance(inner, list):
                out.extend(i for i in inner if isinstance(i, Mapping))
            else:
                out.append(v)
        return out
    if isinstance(value, Mapping):
        for key in (
            "aeop_freight_calculate_result_for_buyer_dtolist",
            "aeop_freight_calculate_result_for_buyer_d_t_o",
            "result",
            "data",
        ):
            item = value.get(key)
            if isinstance(item, (list, Mapping)):
                found = _extract_get_options(item)
                if found:
                    return found
        if any(
            k in value
            for k in (
                "shipping_method",
                "service_name",
                "freight",
                "tracking_available",
            )
        ):
            return [value]
    return []


def _normalize_get_option(option: Mapping[str, Any]) -> AliExpressFreightOption:
    freight = option.get("freight")
    if isinstance(freight, Mapping):
        amount = freight.get("amount")
        cent = freight.get("cent")
        currency = freight.get("currency_code") or freight.get("currency")
    else:
        amount = option.get("freight_amount")
        cent = option.get("freight_cent")
        currency = option.get("currency_code") or option.get("currency")

    # amount é o preço principal; se houver só cent, NÃO converter ainda —
    # apenas registrar presença no diagnóstico e preservar o bruto em raw.
    if amount is not None:
        shipping_fee = parse_price(amount)
    elif cent is not None:
        shipping_fee = None
        logger.info(
            "buyer freight.get: opção sem amount (somente cent); bruto preservado em raw"
        )
    else:
        shipping_fee = None

    if currency is None:
        currency = option.get("currency")

    return AliExpressFreightOption(
        service_name=_as_str(option.get("service_name") or option.get("shipping_method")),
        shipping_fee=shipping_fee,
        currency=_as_str(currency),
        delivery_days=_as_int(option.get("estimated_delivery_time")),
        estimated_delivery=_as_str(option.get("estimated_delivery_time")),
        tracking=_as_bool(option.get("tracking_available")),
        raw=dict(option),
    )


def normalize_buyer_freight_get_response(
    payload: Any, *, method: str
) -> AliExpressFreightResult:
    """Normaliza a resposta de aliexpress.logistics.buyer.freight.get.

    Aciona o wrapper aliexpress_logistics_buyer_freight_get_response e o
    aninhamento aeop_freight_calculate_result_for_buyer_d_t_o. Lança
    AliExpressAPIError se o payload não for objeto.
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError(
            "resposta de buyer.freight.get inesperada (não é objeto)",
            raw=payload,
        )

    options_raw = _extract_get_options(payload)
    options = [_normalize_get_option(o) for o in options_raw]

    currency: Optional[str] = None
    for o in options:
        if o.currency:
            currency = o.currency
            break

    fees = [o.shipping_fee for o in options if o.shipping_fee is not None]
    min_fee = min(fees) if fees else None

    has_days = any(o.delivery_days is not None or o.estimated_delivery for o in options)
    min_days = None
    days = [o.delivery_days for o in options if o.delivery_days is not None]
    if days:
        min_days = min(days)
    elif has_days:
        min_days = None

    return AliExpressFreightResult(
        options=options,
        currency=currency,
        min_fee=min_fee,
        min_delivery_days=min_days,
        raw=dict(payload),
    )


__all__: Iterable[str] = [
    "build_buyer_freight_get_params",
    "normalize_buyer_freight_get_response",
]