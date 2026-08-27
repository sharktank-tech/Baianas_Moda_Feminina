"""Modelos normalizados da busca AliExpress (dataclasses, sem segredos)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, List, Optional


@dataclass
class AliExpressSearchProduct:
    """Produto normalizado de uma busca AliExpress.

    `raw` retém o dict original da API para compatibilidade futura, porém a
    resposta da API nunca deve conter nossas credenciais (app_secret/token/sign).
    """

    external_id: Optional[str] = None
    item_id: Optional[str] = None
    title: Optional[str] = None
    product_url: Optional[str] = None
    images: List[str] = field(default_factory=list)
    original_price: Optional[Decimal] = None
    sale_price: Optional[Decimal] = None
    discount: Optional[str] = None
    orders: Optional[int] = None
    score: Optional[Decimal] = None
    evaluate_rate: Optional[str] = None
    category_id: Optional[str] = None
    video_url: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class AliExpressSearchResult:
    products: List[AliExpressSearchProduct] = field(default_factory=list)
    total: Optional[int] = None
    wrapper: Optional[str] = None
    raw: Optional[dict] = None
