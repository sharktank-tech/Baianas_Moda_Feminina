"""Busca AliExpress: validação, mapeamento de parâmetros e normalização.

A camada pública Python usa snake_case; a serialização TOP usa exatamente:
  keyWord, local, countryCode, categoryId, sortBy, pageSize, pageIndex,
  currency, searchExtend, selectionName
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import AliExpressAPIError, AliExpressConfigError
from .models import AliExpressSearchProduct, AliExpressSearchResult

SUPPORTED_SORT = frozenset(
    {
        "min_price,asc",
        "min_price,desc",
        "orders,asc",
        "orders,desc",
        "comments,asc",
        "comments,desc",
    }
)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_PRICE_CLEAN_RE = re.compile(r"[^\d.,]")
_PRICE_SPLIT_RE = re.compile(r"[.,](?=\d{1,2}$)")
_NUM_RE = re.compile(r"^\d+(\.\d+)?$")


def build_search_params(
    *,
    keyword: Optional[str],
    locale: str,
    country_code: str,
    currency: str,
    category_id: Optional[str],
    sort_by: str,
    page_index: int,
    page_size: int,
    search_extend: Optional[str],
    selection_name: Optional[str],
) -> Dict[str, str]:
    """Valida e mapeia parâmetros Python -> parâmetros de negócio TOP (camelCase)."""
    if not country_code:
        raise AliExpressConfigError("country_code é obrigatório")
    if not locale:
        raise AliExpressConfigError("locale é obrigatório")
    if not _CURRENCY_RE.match(currency or ""):
        raise AliExpressConfigError(f"currency inválido: {currency!r} (esperado 3 letras maiúsculas)")
    if not isinstance(page_index, int) or page_index < 1:
        raise AliExpressConfigError(f"page_index deve ser >= 1 (recebido: {page_index!r})")
    if not isinstance(page_size, int) or page_size <= 0:
        raise AliExpressConfigError(f"page_size deve ser > 0 (recebido: {page_size!r})")
    if sort_by not in SUPPORTED_SORT:
        raise AliExpressConfigError(
            f"sort_by inválido: {sort_by!r}. Suportados: {sorted(SUPPORTED_SORT)}"
        )

    params: Dict[str, str] = {
        "local": locale,
        "countryCode": country_code,
        "currency": currency,
        "sortBy": sort_by,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
    }
    if keyword is not None:
        params["keyWord"] = keyword
    if category_id is not None:
        params["categoryId"] = str(category_id)
    if search_extend is not None:
        params["searchExtend"] = search_extend
    if selection_name is not None:
        params["selectionName"] = selection_name
    return params


def parse_price(value: Any) -> Optional[Decimal]:
    """Converte preço em Decimal; retorna None se não conseguir (sem inventar valor).

    Aceita int/float/Decimal ou string (com símbolo de moeda, espaços ou vírgula
    decimal). Falhas NÃO viram 0.0 silenciosamente.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str):
        cleaned = _PRICE_CLEAN_RE.sub("", value.strip())
        # Normaliza separador decimal: o ÚLTIMO separador é o decimal.
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        if _NUM_RE.match(cleaned):
            try:
                return Decimal(cleaned)
            except (InvalidOperation, ValueError):
                return None
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    if isinstance(value, str):
        # Aceita contagens no formato "10,000+" (live AliExpress).
        s = value.strip().rstrip("+").replace(",", "")
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None
    return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _abs_url(value: Any) -> Optional[str]:
    """Normaliza URLs relativas ao protocolo (//host/...) para https://..."""
    if not isinstance(value, str):
        return None
    if value.startswith("//"):
        return "https:" + value
    return value


def _as_images(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (list, tuple)):
        return [_abs_url(v) for v in value if v]
    return []


def normalize_product(raw: Mapping[str, Any]) -> AliExpressSearchProduct:
    """Normaliza um item de produto da resposta (tolerante a campos opcionais)."""
    return AliExpressSearchProduct(
        external_id=_as_str(raw.get("itemId") or raw.get("productId")),
        item_id=_as_str(raw.get("itemId")),
        title=_as_str(raw.get("title") or raw.get("subject")),
        product_url=_abs_url(raw.get("itemUrl") or raw.get("productUrl") or raw.get("product_url")),
        images=_as_images(raw.get("itemMainPic") or raw.get("imageUrl")),
        original_price=parse_price(
            raw.get("targetOriginalPrice") or raw.get("originalPrice") or raw.get("original_price")
        ),
        sale_price=parse_price(
            raw.get("targetSalePrice") or raw.get("salePrice") or raw.get("sale_price")
        ),
        discount=_as_str(raw.get("discount")),
        orders=_as_int(raw.get("orders") or raw.get("volume")),
        score=(parse_price(raw.get("score")) if raw.get("score") is not None else None),
        evaluate_rate=_as_str(raw.get("evaluateRate") or raw.get("evaluate_rate")),
        category_id=_as_str(raw.get("categoryId") or raw.get("category_id")),
        video_url=_abs_url(raw.get("videoUrl") or raw.get("video_url")),
        raw=dict(raw),
    )


def _extract_product_list(payload: Mapping[str, Any]) -> List[Any]:
    products = payload.get("products")
    if isinstance(products, list):
        return products
    # Formato live: {"products": {"selection_search_product": [...]}}
    if isinstance(products, dict):
        items = products.get("selection_search_product")
        if isinstance(items, list):
            return items
    for cand in ("itemList", "items", "productList"):
        v = payload.get(cand)
        if isinstance(v, list):
            return v
    result = payload.get("result")
    if isinstance(result, dict):
        for cand in ("products", "itemList", "items"):
            v = result.get(cand)
            if isinstance(v, list):
                return v
    return []


def _extract_total(payload: Mapping[str, Any]) -> Optional[int]:
    if "totalCount" in payload and payload["totalCount"] is not None:
        return _as_int(payload["totalCount"])
    if "total" in payload and payload["total"] is not None:
        return _as_int(payload["total"])
    result = payload.get("result")
    if isinstance(result, dict) and result.get("total") is not None:
        return _as_int(result["total"])
    return None


def normalize_search_response(
    payload: Mapping[str, Any], *, method: str
) -> AliExpressSearchResult:
    """Converte o payload de resposta (já decodificado) em AliExpressSearchResult.

    Suporta o formato live em que os resultados vêm dentro de `data`
    (ex.: {"code": "00", "data": {"totalCount": N, "products": {...}}}).
    """
    if not isinstance(payload, Mapping):
        raise AliExpressAPIError("resposta de busca inesperada (não é objeto)", raw=payload)

    effective = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload

    raw_items = _extract_product_list(effective)
    products = [normalize_product(it) for it in raw_items if isinstance(it, Mapping)]
    total = _extract_total(effective)

    if total is None:
        total = len(products)

    return AliExpressSearchResult(
        products=products,
        total=total,
        wrapper=f"{method}_response",
        raw=dict(payload),
    )
