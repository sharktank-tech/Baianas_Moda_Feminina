"""Frete AliExpress (Fase 4, read-only, sem persistência).

Normaliza a resposta de `aliexpress.ds.freight.query`. O formato live exato
ainda está sendo consolidado, por isso o normalizador é tolerante a:

  - wrapper de lista: {"freightList": [...]} / {"freight_list": [...]}
  - lista direta: [...]
  - opção única (dict) diretamente no payload
  - níveis extras de envelope (result/data)

O parâmetro de negócio `queryDeliveryReq` é um JSON compacto (determinístico),
serializado com `separators=(",", ":")`, e participa normalmente da assinatura
/sync. Campos de frete são preservados em `raw`; valores monetários em Decimal.
Não dividimos valores automaticamente só por causa do nome (ex.: shipping_fee_cent).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, List, Mapping, Optional

from .errors import AliExpressAPIError
from .models import AliExpressFreightOption, AliExpressFreightResult
from .search import _as_int, _as_str, parse_price


def build_freight_params(
    *,
    product_id: str,
    selected_sku_id: str,
    ship_to_country: str = "BR",
    currency: str = "BRL",
    quantity: int = 1,
    province_code: Optional[str] = None,
    city_code: Optional[str] = None,
    language: str = "en_US",
    locale: Optional[str] = None,
) -> dict:
    """Mapeia parâmetros Python -> negócio de ds.freight.query.

    `queryDeliveryReq` é o JSON compacto determinístico exigido pela API.
    """
    if not product_id:
        raise ValueError("product_id é obrigatório")
    if not selected_sku_id:
        raise ValueError("selected_sku_id é obrigatório")
    if not ship_to_country:
        raise ValueError("ship_to_country (country) é obrigatório")
    if not currency:
        raise ValueError("currency é obrigatório")
    if not isinstance(quantity, int) or quantity < 1:
        raise ValueError("quantity deve ser um int >= 1")

    req = {
        "productId": product_id,
        "selectedSkuId": selected_sku_id,
        "shipToCountry": ship_to_country,
        "provinceCode": province_code or "",
        "cityCode": city_code or "",
        "currency": currency,
        "quantity": quantity,
        "source": "",
        "language": language,
        "locale": locale or "",
    }
    # JSON compacto e determinístico (ordem das chaves fixa).
    return {"queryDeliveryReq": json.dumps(req, separators=(",", ":"))}


def _opt(value: Mapping[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in value and value[k] is not None:
            return value[k]
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "y", "yes")
    return bool(value)


def _extract_freight_options(value: Any) -> List[dict]:
    """Localiza a lista de opções de frete em formatos variados (tolerante)."""
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in (
            "freight_list",
            "freightList",
            "freightOptions",
            "shipping_options",
            "options",
            "list",
            "result",
            "data",
            "freight",
        ):
            item = value.get(key)
            if isinstance(item, (list, Mapping)):
                found = _extract_freight_options(item)
                if found:
                    return found
        # Dict único que parece uma opção de frete.
        if any(
            k in value
            for k in (
                "serviceName",
                "freightFee",
                "shippingFee",
                "company",
                "deliveryDays",
                "freight_list",
            )
        ):
            return [value]
    return []


def _normalize_freight_option(option: Mapping[str, Any]) -> AliExpressFreightOption:
    return AliExpressFreightOption(
        service_name=_as_str(_opt(option, "serviceName", "service_name", "service")),
        company=_as_str(
            _opt(option, "company", "companyName", "company_name", "logisticsCompany")
        ),
        shipping_fee=parse_price(
            _opt(
                option,
                "shippingFee",
                "freightFee",
                "shipping_fee",
                "freight_fee",
                "fee",
            )
        ),
        currency=_as_str(_opt(option, "currency", "currencyCode", "currency_code")),
        delivery_days=_as_int(
            _opt(
                option,
                "deliveryDays",
                "deliveryTime",
                "delivery_days",
                "estimatedDeliveryTime",
                "deliveryTimeRange",
            )
        ),
        estimated_delivery=_as_str(
            _opt(option, "estimatedDelivery", "estimated_delivery", "estimatedDeliveryDate")
        ),
        tracking=_as_bool(_opt(option, "tracking", "trackingFlag", "trackable")),
        warehouse=_as_str(_opt(option, "warehouse", "warehouseName", "warehouse_name")),
        country=_as_str(
            _opt(option, "country", "countryCode", "dispatchCountry", "shipFromCountry")
        ),
        raw=dict(option),
    )


def normalize_freight_response(
    payload: Mapping[str, Any], *, method: str
) -> AliExpressFreightResult:
    """Converte o payload (já decodificado) em AliExpressFreightResult.

    Tolerante a wrappers/formatos live; preserva `raw`. Não inventa valores.
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError("resposta de frete inesperada (não é objeto)", raw=payload)

    options_raw = _extract_freight_options(payload)
    options = [
        _normalize_freight_option(o) for o in options_raw if isinstance(o, Mapping)
    ]

    currency = None
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


__all__: List[str] = [
    "build_freight_params",
    "normalize_freight_response",
    "AliExpressFreightResult",
    "AliExpressFreightOption",
]
