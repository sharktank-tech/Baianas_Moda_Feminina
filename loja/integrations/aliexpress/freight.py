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


def _first_not_none(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _is_success_value(value: Any) -> Optional[bool]:
    """Interpreta success como bool, aceitando strings ('true'/'false'/'0'/'1').

    Retorna None se não conseguir determinar (para cair no fallback de code).
    """
    if value is True:
        return True
    if value is False:
        return False
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes"}:
            return True
        if v in {"false", "0", "no"}:
            return False
    return None


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
    """Localiza a lista de opções de frete em formatos variados (tolerante).

    Desce por wrappers conhecidos (result/data/freight/delivery/opções) sem
    interpretar qualquer dict arbitrário como opção. Um dict folha só é tratado
    como opção única se contiver campos típicos de frete.
    """
    _WRAPPER_KEYS = (
        "freight_list",
        "freightList",
        "freightOptions",
        "shipping_options",
        "options",
        "list",
        "result",
        "data",
        "freight",
        "delivery",
        "delivery_options",
        "deliveryOptions",
        "delivery_option_list",
        "deliveryOptionList",
        "shipping_methods",
        "shippingMethods",
    )
    _OPTION_FIELD_KEYS = (
        "service_name",
        "serviceName",
        "shipping_fee",
        "shipping_fee_cent",
        "shippingFee",
        "shippingFeeCent",
        "freight_fee",
        "freightFee",
        "company",
        "delivery_days",
        "min_delivery_days",
        "max_delivery_days",
        "deliveryDays",
        "delivery_date_desc",
    )
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in _WRAPPER_KEYS:
            item = value.get(key)
            if isinstance(item, (list, Mapping)):
                found = _extract_freight_options(item)
                if found:
                    return found
        # Dict folha que parece uma opção de frete.
        if any(k in value for k in _OPTION_FIELD_KEYS):
            return [value]
    return []


def _normalize_freight_option(option: Mapping[str, Any]) -> AliExpressFreightOption:
    # Prazo: prefere min_delivery_days; cai para max/guaranteed/deliveryDays/delivery_days.
    delivery_days = _as_int(
        _first_not_none(
            option.get("min_delivery_days"),
            option.get("max_delivery_days"),
            option.get("guaranteed_delivery_days"),
            option.get("deliveryDays"),
            option.get("delivery_days"),
        )
    )
    return AliExpressFreightOption(
        service_name=_as_str(
            _opt(option, "serviceName", "service_name", "code")
        ),
        company=_as_str(
            _opt(option, "company", "companyName", "company_name", "logisticsCompany")
        ),
        # Não dividimos valores_cent por 100; parseamos como estão.
        shipping_fee=parse_price(
            _opt(
                option,
                "shipping_fee_cent",
                "shippingFeeCent",
                "shipping_fee",
                "shippingFee",
                "freight_fee",
                "freightFee",
            )
        ),
        currency=_as_str(
            _opt(option, "shipping_fee_currency", "currency", "currency_code", "currencyCode")
        ),
        delivery_days=delivery_days,
        estimated_delivery=_as_str(
            _opt(
                option,
                "delivery_date_desc",
                "delivery_date",
                "estimatedDelivery",
                "estimatedDeliveryDate",
            )
        ),
        tracking=_as_bool(_opt(option, "tracking", "tracking_code", "trackingFlag", "trackable")),
        warehouse=_as_str(_opt(option, "warehouse", "warehouseName", "warehouse_name")),
        country=_as_str(
            _opt(option, "ship_from_country", "dispatchCountry", "country", "countryCode")
        ),
        raw=dict(option),
    )


def normalize_freight_response(
    payload: Mapping[str, Any], *, method: str
) -> AliExpressFreightResult:
    """Converte o payload (já decodificado) em AliExpressFreightResult.

    Tolerante a wrappers/formatos live; preserva `raw`. Não inventa valores.
    Levanta AliExpressAPIError em erro de negócio explícito (success=false ou
    code != 0), para que o callback exiba 'DS freight.query: failure'.
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError("resposta de frete inesperada (não é objeto)", raw=payload)

    # Erro de negócio explícito antes de extrair opções (shape live:
    # {"result": {"msg":..., "code":..., "success":...}, "request_id":...}).
    result = payload.get("result")
    if isinstance(result, Mapping):
        parsed_success = _is_success_value(result.get("success"))
        if parsed_success is False:
            code = result.get("code")
            msg = result.get("msg")
            raise AliExpressAPIError(
                str(msg or "AliExpress freight query failed"),
                code=code,
                raw=payload,
            )
        if parsed_success is None:
            # Fallback: code != 0 indica erro (mas success=True tem prioridade).
            code = result.get("code")
            if code is not None:
                s = str(code).strip()
                if s and not (s.isdigit() and int(s) == 0):
                    msg = result.get("msg")
                    raise AliExpressAPIError(
                        str(msg or "AliExpress freight query failed"),
                        code=code,
                        raw=payload,
                    )

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
