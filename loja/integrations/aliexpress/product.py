"""Detalhe de produto AliExpress (Fase 3, read-only, sem persistência).

Mapeia a chamada `aliexpress.ds.product.get` e normaliza o resultado em
`AliExpressProductDetail`. Os nomes exatos dos parâmetros de negócio e da
resposta serão consolidados após observarmos o formato live; por ora usamos
os nomes documentados da Open Platform.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from .errors import AliExpressAPIError
from .models import AliExpressProductDetail, AliExpressProductSku
from .search import _as_images, _as_int, _as_str, _abs_url, parse_price


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


def _normalize_attributes(value: Any) -> List[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[dict] = []
    for attr in value:
        if isinstance(attr, Mapping):
            name = attr.get("name") or attr.get("attrName") or attr.get("attributeName")
            val = attr.get("value") or attr.get("attrValue") or attr.get("attributeValue")
            out.append({"name": _as_str(name), "value": _as_str(val)})
        else:
            out.append({"name": None, "value": _as_str(attr)})
    return out


def _normalize_skus(value: Any) -> List[AliExpressProductSku]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[AliExpressProductSku] = []
    for sku in value:
        if not isinstance(sku, Mapping):
            continue
        out.append(
            AliExpressProductSku(
                sku_id=_as_str(sku.get("skuId") or sku.get("id")),
                attributes=_normalize_attributes(
                    sku.get("attributes") or sku.get("skuAttrs") or sku.get("properties")
                ),
                available_stock=_as_int(
                    sku.get("availableStock") or sku.get("stock") or sku.get("quantity")
                ),
                sale_price=parse_price(
                    sku.get("salePrice") or sku.get("skuPrice") or sku.get("price")
                ),
                original_price=parse_price(sku.get("originalPrice") or sku.get("skuOriginalPrice")),
                currency=_as_str(sku.get("currency") or sku.get("currencyCode")),
                raw=dict(sku),
            )
        )
    return out


def _extract_product_object(payload: Mapping[str, Any]) -> Any:
    if isinstance(payload.get("product"), Mapping):
        return payload["product"]
    result = payload.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("product"), Mapping):
        return result["product"]
    # Às vezes o próprio payload já é o objeto do produto.
    return payload


def normalize_product_detail(payload: Mapping[str, Any], *, method: str) -> AliExpressProductDetail:
    """Converte o payload de resposta (já decodificado) em AliExpressProductDetail.

    Suporta o envelope live `{"code": "00", "data": {"product": {...}}}`.
    Campos desconhecidos são preservados em `raw` (sem inventar valores).
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError("resposta de produto inesperada (não é objeto)", raw=payload)

    effective = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    product = _extract_product_object(effective)
    if not isinstance(product, Mapping):
        raise AliExpressAPIError("resposta de produto sem objeto 'product'", raw=payload)

    return AliExpressProductDetail(
        product_id=_as_str(
            product.get("productId") or product.get("itemId") or product.get("product_id")
        ),
        title=_as_str(product.get("title") or product.get("subject")),
        description=_as_str(
            product.get("description") or product.get("detail") or product.get("summary")
        ),
        category_id=_as_str(product.get("categoryId") or product.get("category_id")),
        images=_as_images(product.get("images") or product.get("imageUrl") or product.get("mainImage")),
        video=_abs_url(product.get("video") or product.get("videoUrl")),
        skus=_normalize_skus(product.get("sku") or product.get("skus")),
        attributes=_normalize_attributes(product.get("attributes") or product.get("properties")),
        available_stock=_as_int(
            product.get("availableStock") or product.get("stock") or product.get("quantity")
        ),
        sale_price=parse_price(
            product.get("salePrice") or product.get("minSalePrice") or product.get("sale_price")
        ),
        original_price=parse_price(
            product.get("originalPrice") or product.get("minOriginalPrice") or product.get("original_price")
        ),
        currency=_as_str(product.get("currency") or product.get("currencyCode")),
        taxes=product.get("taxes"),
        raw=dict(product),
    )


__all__: List[str] = [
    "build_product_detail_params",
    "normalize_product_detail",
    "AliExpressProductDetail",
    "AliExpressProductSku",
]
