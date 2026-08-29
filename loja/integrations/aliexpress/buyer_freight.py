"""Frete oficial do buyer (Fase 4b, read-only, sem persistência).

Implementa aliexpress.logistics.buyer.freight.calculate. Primeiro testamos se o
gateway atual /sync (sign_method=sha256, session=token) aceita esse método —
a documentação antiga ainda aponta o TOP legado com hmac/md5. Se o Open Platform
responder "method not found"/"permission denied"/etc., o erro é preservado e
sanitizado pela camada de transporte (client.call -> _parse_response).
"""

import json
import logging
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional

from .errors import AliExpressAPIError
from .models import AliExpressFreightOption, AliExpressFreightResult
from .search import _as_int, _as_str, parse_price

logger = logging.getLogger(__name__)

# Chaves cujos valores NUNCA devem aparecer nos logs de diagnóstico.
_SENSITIVE_KEYS = {
    "request_id",
    "requestid",
    "sign",
    "session",
    "access_token",
    "app_secret",
    "token",
}


def build_buyer_freight_params(
    *,
    product_id: str,
    country_code: str = "BR",
    product_num: int = 1,
    send_goods_country_code: str = "CN",
    province_code: Optional[str] = None,
    city_code: Optional[str] = None,
    sku_id: Optional[str] = None,
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
    try:
        product_id_num = int(product_id)
    except (TypeError, ValueError):
        raise ValueError("product_id deve ser numérico para buyer.freight.calculate")
    if not isinstance(product_num, int) or product_num < 1:
        raise ValueError("product_num deve ser um inteiro >= 1")

    dto: dict = {
        "country_code": country_code,
        "product_id": product_id_num,
        "product_num": int(product_num),
        "send_goods_country_code": send_goods_country_code,
    }
    if province_code:
        dto["province_code"] = province_code
    if city_code:
        dto["city_code"] = city_code
    if sku_id:
        dto["sku_id"] = str(sku_id)
    if price is not None and price_currency is not None:
        dto["price"] = str(price)
        dto["price_currency"] = price_currency

    business_param = json.dumps(dto, separators=(",", ":"))
    return {"param_aeop_freight_calculate_for_buyer_d_t_o": business_param}


def _parse_bool(value: Any) -> Optional[bool]:
    """Interpreta sucesso/erro como bool de forma robusta.

    false / "false" / 0 / "0" -> False
    true  / "true"  / 1 / "1" -> True
    Outros -> None (não confundir string "false" com truthy).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y"):
            return True
        if v in ("false", "0", "no", "n"):
            return False
    return None


def _safe_keys(obj: Any) -> Any:
    """Lista apenas nomes de chaves (tipos/tamanhos), nunca valores sensíveis."""
    if not isinstance(obj, Mapping):
        return type(obj).__name__
    return sorted(k for k in obj.keys() if k.lower() not in _SENSITIVE_KEYS)


def _find_result_obj(value: Any) -> Optional[Mapping[str, Any]]:
    """Localiza o objeto de resultado de negócio (success/error_desc/lista)."""
    if not isinstance(value, Mapping):
        return None
    if any(
        k in value
        for k in (
            "success",
            "error_desc",
            "aeop_freight_calculate_result_for_buyer_d_t_o_list",
            "aeop_freight_calculate_result_for_buyer_dto",
        )
    ):
        return value
    for key in ("result", "data"):
        item = value.get(key)
        if isinstance(item, Mapping):
            found = _find_result_obj(item)
            if found is not None:
                return found
    return None


def _find_list_wrapper(value: Any) -> Optional[Mapping[str, Any]]:
    """Localiza o wrapper que contém a lista de opções (para log de diagnóstico)."""
    if isinstance(value, Mapping):
        if "aeop_freight_calculate_result_for_buyer_dto" in value:
            return value
        for key in (
            "result",
            "data",
            "aeop_freight_calculate_result_for_buyer_d_t_o_list",
        ):
            item = value.get(key)
            if isinstance(item, Mapping):
                found = _find_list_wrapper(item)
                if found is not None:
                    return found
    return None


def _extract_buyer_options(value: Any) -> List[Mapping[str, Any]]:
    """Navega pelos wrappers oficiais até a lista de opções de frete.

    Suporta:
      - lista direta
      - result[data] -> aeop_freight_calculate_result_for_buyer_d_t_o_list
        (que pode ser a própria lista ou um dict com
         aeop_freight_calculate_result_for_buyer_dto)
      - opção única (dict) diretamente no payload
    """
    if isinstance(value, list):
        return [v for v in value if isinstance(v, Mapping)]
    if isinstance(value, Mapping):
        for key in (
            "aeop_freight_calculate_result_for_buyer_d_t_o_list",
            "aeop_freight_calculate_result_for_buyer_dto",
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


def _is_valid_buyer_option(option: Mapping[str, Any]) -> bool:
    """error_code == 0 => válida; não-zero => inválida (erro no candidato)."""
    ec = option.get("error_code")
    if ec is None:
        return True
    try:
        return int(ec) == 0
    except (TypeError, ValueError):
        return False


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
    result[...]. Valida `success` explícito (False => erro de negócio) e filtra
    opções com error_code != 0. Lança AliExpressAPIError se `payload` não for
    objeto ou se todas as opções retornarem erro.
    """
    if not isinstance(payload, (Mapping, list)):
        raise AliExpressAPIError(
            "resposta de buyer.freight.calculate inesperada (não é objeto)",
            raw=payload,
        )

    result_obj = None
    list_wrapper = None
    if isinstance(payload, Mapping):
        result_obj = _find_result_obj(payload)
        list_wrapper = _find_list_wrapper(payload)

        # Erro de negócio explícito antes de extrair opções.
        if result_obj is not None:
            success = _parse_bool(result_obj.get("success"))
            if success is False:
                error_desc = (
                    result_obj.get("error_desc")
                    or result_obj.get("msg")
                    or result_obj.get("error_message")
                )
                code = result_obj.get("error_code") or result_obj.get("code")
                raise AliExpressAPIError(
                    str(error_desc or "buyer freight calculation failed"),
                    code=code,
                    raw=payload,
                )

    options_raw = _extract_buyer_options(payload)
    filtered = [
        o for o in options_raw if isinstance(o, Mapping) and _is_valid_buyer_option(o)
    ]
    # Opções vieram, mas todas com erro => indisponibilidade coerente (não sucesso).
    if options_raw and not filtered:
        raise AliExpressAPIError(
            "todas as opções de frete retornaram erro", raw=payload
        )

    options = [_normalize_buyer_option(o) for o in filtered]

    # Diagnóstico temporário: 0 opções após correção. Só estrutura, sem valores.
    if not options:
        logger.info(
            "buyer freight returned 0 options. shape: keys=%s result.keys=%s list_wrapper.keys=%s",
            _safe_keys(payload),
            _safe_keys(result_obj),
            _safe_keys(list_wrapper),
        )

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
        raw=dict(payload) if isinstance(payload, Mapping) else payload,
    )


__all__: Iterable[str] = [
    "build_buyer_freight_params",
    "normalize_buyer_freight_response",
]
