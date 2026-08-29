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

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from .errors import AliExpressAPIError
from .models import AliExpressProductDetail, AliExpressProductSku
from .search import _as_int, _as_str, _abs_url, parse_price


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


def _normalize_attributes(container: Any) -> List[dict]:
    """Atributos gerais: ae_item_properties.ae_item_property[].{attr_name,attr_value}."""
    if not isinstance(container, Mapping):
        return []
    items = container.get("ae_item_property") or []
    out: List[dict] = []
    for attr in items:
        if not isinstance(attr, Mapping):
            continue
        name = attr.get("attr_name")
        value = attr.get("attr_value")
        out.append({"name": _as_str(name), "value": _as_str(value)})
    return out


def _normalize_sku_attributes(sku: Mapping[str, Any]) -> List[dict]:
    """Propriedades do SKU (em um de dois containers conhecidos)."""
    container = sku.get("ae_sku_property_dtos") or sku.get("aeop_s_k_u_propertys") or {}
    items = container.get("ae_sku_property_d_t_o") or []
    out: List[dict] = []
    for attr in items:
        if not isinstance(attr, Mapping):
            continue
        name = attr.get("sku_property_name")
        value = attr.get("property_value_definition_name") or attr.get("sku_property_value")
        out.append({"name": _as_str(name), "value": _as_str(value)})
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


def _normalize_images(multimedia: Mapping[str, Any]) -> List[str]:
    """image_urls pode vir como string (separada por ';') ou lista."""
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
    multimedia = result.get("ae_multimedia_info_dto") or {}
    sku_container = result.get("ae_item_sku_info_dtos") or {}
    sku_items = sku_container.get("ae_item_sku_info_d_t_o") or []
    props = result.get("ae_item_properties") or {}

    return AliExpressProductDetail(
        product_id=_as_str(base.get("product_id")),
        title=_as_str(base.get("subject")),
        description=_as_str(base.get("detail")),
        category_id=_as_str(base.get("category_id")),
        images=_normalize_images(multimedia),
        video=None,  # só preencher se houver URL real; dados de mídia ficam em raw
        skus=_normalize_skus(sku_items),
        attributes=_normalize_attributes(props),
        available_stock=None,  # a API fornece estoque por SKU
        sale_price=None,  # a API fornece preço por SKU
        original_price=None,
        currency=_as_str(base.get("currency_code")),
        taxes=None,
        raw=dict(result),
    )


__all__: List[str] = [
    "build_product_detail_params",
    "normalize_product_detail",
    "first_not_none",
    "AliExpressProductDetail",
    "AliExpressProductSku",
]
