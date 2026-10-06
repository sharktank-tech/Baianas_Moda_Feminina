"""Detalhe de produto AliExpress (Fase 3, read-only, sem persistência).

Normaliza a resposta oficial de `aliexpress.ds.product.get`, cujo shape é:

    {
        "result": {
            "ae_item_base_info_dto": {...},
            "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [...]},
            "ae_item_properties": {"ae_item_property": [...]},
            "ae_multimedia_info_dto": {...},
            ...
        },
        "rsp_code": "...",
        "rsp_msg": "..."
    }

Os nomes exatos de campos foram consolidados após observarmos o formato live.
Campos desconhecidos são preservados em `raw` (sem inventar valores).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from .errors import AliExpressAPIError
from .models import AliExpressProductDetail, AliExpressProductSku
from .search import _as_int, _as_str, _abs_url, parse_price


#: IDs de produto AliExpress são numéricos (ex.: 1005013003211750).
#: A faixa aceita cobre os formatos curtos legados e os longos atuais,
#: sem aceitar valores que claramente não são IDs (preços, quantidades).
_PRODUCT_ID_RE_MIN_DIGITS = 8
_PRODUCT_ID_RE_MAX_DIGITS = 20

#: Hosts explicitamente suportados para consulta por URL. Qualquer outro
#: host é recusado: nunca fazemos requisições arbitrárias a partir de URLs
#: fornecidas no painel.
_ALIEXPRESS_HOSTS = ("aliexpress.com", "aliexpress.us")


def parse_aliexpress_product_ref(value: Any) -> Optional[str]:
    """Extrai o ID de produto de um ID avulso ou URL de anúncio AliExpress.

    Aceita:
      - ID numérico (`1005013003211750`);
      - URL de anúncio AliExpress (`https://pt.aliexpress.com/item/<id>.html`,
        incluindo subdomínios de loja/idioma e query strings).

    Qualquer outra coisa (host fora da AliExpress, caminho que não seja
    `/item/<id>.html`, ID não numérico, string vazia) devolve None.
    Função pura: nunca toca na rede.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return text if _looks_like_product_id(text) else None
    if "://" not in text:
        return None
    try:
        parsed = urlparse(text)
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if not host or not any(
        host == base or host.endswith("." + base) for base in _ALIEXPRESS_HOSTS
    ):
        return None
    match = re.match(r"^/item/(\d+)\.html?$", parsed.path or "")
    if not match:
        return None
    candidate = match.group(1)
    return candidate if _looks_like_product_id(candidate) else None


def _looks_like_product_id(candidate: str) -> bool:
    return (
        candidate.isdigit()
        and _PRODUCT_ID_RE_MIN_DIGITS <= len(candidate) <= _PRODUCT_ID_RE_MAX_DIGITS
    )


def first_not_none(*values: Any) -> Any:
    """Retorna o primeiro valor que não é None (estoque 0 é válido)."""
    for v in values:
        if v is not None:
            return v
    return None


def build_product_detail_params(
    *,
    product_id: str,
    ship_to_country: str,
    target_currency: str,
    target_language: str,
    remove_personal_benefit: bool,
    biz_model: Optional[str],
    province: Optional[str],
    city: Optional[str],
) -> Dict[str, str]:
    """Mapeia parâmetros Python -> parâmetros de negócio de ds.product.get."""
    params: Dict[str, str] = {
        "product_id": str(product_id),
        "ship_to_country": ship_to_country,
        "target_currency": target_currency,
        "target_language": target_language,
        "remove_personal_benefit": "true" if remove_personal_benefit else "false",
    }
    if biz_model is not None:
        params["biz_model"] = biz_model
    if province is not None:
        params["province"] = province
    if city is not None:
        params["city"] = city
    return params


def _extract_result(payload: Mapping[str, Any]) -> Any:
    """Localiza o objeto `result` da resposta (com ou sem envelope `data`)."""
    result = payload.get("result")
    if isinstance(result, Mapping):
        return result

    data = payload.get("data")
    if isinstance(data, Mapping):
        result = data.get("result")
        if isinstance(result, Mapping):
            return result
        return data

    return payload


def _extract_sku_items(value: Any) -> List[dict]:
    """ae_item_sku_info_dtos tolera lista direta (simplify=true) ou wrapper."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        items = (
            value.get("ae_item_sku_info_d_t_o")
            or value.get("ae_item_sku_info_dto")
            or value.get("items")
        )
        if isinstance(items, list):
            return items
        if isinstance(items, tuple):
            return list(items)
        if isinstance(items, Mapping):
            return [items]
    return []


def _extract_property_items(value: Any) -> List[dict]:
    """ae_item_properties tolera lista direta ou wrapper {ae_item_property:[...]}."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        items = value.get("ae_item_property") or value.get("items")
        if isinstance(items, list):
            return items
        if isinstance(items, tuple):
            return list(items)
        if isinstance(items, Mapping):
            return [items]
    return []


def _extract_sku_property_items(value: Any) -> List[dict]:
    """Propriedades de SKU toleram lista direta ou wrapper {ae_sku_property_d_t_o:[...]}."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        items = (
            value.get("ae_sku_property_d_t_o")
            or value.get("ae_sku_property_dto")
            or value.get("items")
        )
        if isinstance(items, list):
            return items
        if isinstance(items, tuple):
            return list(items)
        if isinstance(items, Mapping):
            return [items]
    return []


def _normalize_attributes(items: Any) -> List[dict]:
    """Atributos gerais: [{attr_name, attr_value}, ...]."""
    if not isinstance(items, (list, tuple)):
        return []
    out: List[dict] = []
    for attr in items:
        if not isinstance(attr, Mapping):
            continue
        name = attr.get("attr_name")
        value = attr.get("attr_value")
        out.append({"name": _as_str(name), "value": _as_str(value)})
    return out


def _normalize_sku_attributes(sku: Mapping[str, Any]) -> List[dict]:
    """Propriedades do SKU preservando identidade necessária ao placeorder.

    Fase 8.0.10: além de ``name``/``value`` usados pelo storefront, preserva
    ``sku_property_id`` e ``property_value_id`` quando o provider os informa.
    Esses IDs formam o ``sku_attr`` exigido por ``aliexpress.trade.buy.placeorder``
    (ex.: ``14:771;5:9``). Nenhum ID é inventado quando ausente.
    """
    container = sku.get("ae_sku_property_dtos") or sku.get("aeop_s_k_u_propertys")
    items = _extract_sku_property_items(container)
    out: List[dict] = []
    for attr in items:
        if not isinstance(attr, Mapping):
            continue
        name = attr.get("sku_property_name")
        value = attr.get("property_value_definition_name") or attr.get("sku_property_value")
        normalized = {
            "name": _as_str(name),
            "value": _as_str(value),
        }
        property_id = _as_str(attr.get("sku_property_id"))
        property_value_id = _as_str(
            first_not_none(
                attr.get("property_value_id"),
                attr.get("property_value_id_long"),
            )
        )
        if property_id:
            normalized["sku_property_id"] = property_id
        if property_value_id:
            normalized["property_value_id"] = property_value_id
        out.append(normalized)
    return out


def _normalize_skus(items: Any) -> List[AliExpressProductSku]:
    if not isinstance(items, (list, tuple)):
        return []
    out: List[AliExpressProductSku] = []
    for sku in items:
        if not isinstance(sku, Mapping):
            continue
        out.append(
            AliExpressProductSku(
                sku_id=_as_str(sku.get("id")),
                attributes=_normalize_sku_attributes(sku),
                # Estoque 0 é válido -> first_not_none (não `or`).
                available_stock=_as_int(
                    first_not_none(
                        sku.get("sku_available_stock"),
                        sku.get("s_k_u_available_stock"),
                        sku.get("ipm_sku_stock"),
                    )
                ),
                sale_price=parse_price(
                    first_not_none(sku.get("offer_sale_price"), sku.get("offer_bulk_sale_price"))
                ),
                original_price=parse_price(sku.get("sku_price")),
                currency=_as_str(sku.get("currency_code")),
                raw=dict(sku),
            )
        )
    return out


def _normalize_images(multimedia: Any) -> List[str]:
    """Multimídia tolera: wrapper {image_urls}, string, lista de URLs ou de objetos."""
    if multimedia is None:
        return []
    if isinstance(multimedia, str):
        parts = [p for p in multimedia.split(";") if p]
        return [_abs_url(p) for p in parts]
    if isinstance(multimedia, (list, tuple)):
        out: List[str] = []
        for item in multimedia:
            if isinstance(item, str):
                out.append(_abs_url(item))
            elif isinstance(item, Mapping):
                url = (
                    item.get("image_url")
                    or item.get("url")
                    or item.get("img_url")
                    or item.get("abs_url")
                )
                if url:
                    out.append(_abs_url(url))
        return out
    if isinstance(multimedia, Mapping):
        raw_urls = multimedia.get("image_urls")
        if raw_urls is None:
            return []
        if isinstance(raw_urls, str):
            parts = [p for p in raw_urls.split(";") if p]
        elif isinstance(raw_urls, (list, tuple)):
            parts = [p for p in raw_urls if p]
        else:
            return []
        return [_abs_url(p) for p in parts]
    return []


def normalize_product_detail(payload: Mapping[str, Any], *, method: str) -> AliExpressProductDetail:
    """Converte o payload de resposta (já decodificado) em AliExpressProductDetail.

    Suporta o envelope live `{"result": {...}, "rsp_code": ..., "rsp_msg": ...}`.
    O `result` inteiro é preservado em `raw`.
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError("resposta de produto inesperada (não é objeto)", raw=payload)

    result = _extract_result(payload)
    if not isinstance(result, Mapping):
        raise AliExpressAPIError("resposta de produto sem objeto 'result'", raw=payload)

    base = result.get("ae_item_base_info_dto") or {}
    multimedia = result.get("ae_multimedia_info_dto")
    sku_items = _extract_sku_items(result.get("ae_item_sku_info_dtos"))
    property_items = _extract_property_items(result.get("ae_item_properties"))

    return AliExpressProductDetail(
        product_id=_as_str(base.get("product_id")),
        title=_as_str(base.get("subject")),
        description=_as_str(base.get("detail")),
        category_id=_as_str(base.get("category_id")),
        images=_normalize_images(multimedia),
        video=None,
        skus=_normalize_skus(sku_items),
        attributes=_normalize_attributes(property_items),
        currency=_as_str(base.get("currency_code")),
        raw=dict(result),
    )


__all__ = [
    "build_product_detail_params",
    "normalize_product_detail",
    "first_not_none",
    "parse_aliexpress_product_ref",
]
