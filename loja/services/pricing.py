"""Precificação de dropshipping (Fase 6).

Semântica financeira canônica (NÃO misturar):
    supplier cost = SupplierProduct.cost            (fonte canônica)
    shipping cost = SupplierProduct.shipping_cost
    landed cost   = supplier cost + shipping cost  (calculado por aqui)
    selling price = Product.price

Nesta fase ficam FORA do landed cost: impostos, FX, gateway fee, parcelamento,
desconto e comissão. Nunca adicionamos valor presumido para nenhum deles.

Definições explícitas:
    margem (%) = (preço_venda - landed_cost) / preço_venda * 100
    markup (%) = (preço_venda - landed_cost) / landed_cost * 100

Para margem alvo M (%):
    preço_venda = landed_cost / (1 - M / 100)

Sem margem padrão: target_margin_percent deve ser fornecida explicitamente.
Se estiver NULL, o produto não pode ser precificado nem publicado.

Nenhum cálculo usa float; toda entrada é normalizada via Decimal(str(value)).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Optional

from loja.modules.models import Product, SupplierProduct
from loja.utils import utcnow

logger = logging.getLogger(__name__)

SOURCE_ALIEXPRESS = "aliexpress"
FREIGHT_AVAILABLE = "available"

# Estados permitidos de products.pricing_status.
UNPRICED = "unpriced"
BLOCKED = "blocked"
READY = "ready"
PRICED = "priced"
STALE = "stale"

PRICING_STATUSES = {UNPRICED, BLOCKED, READY, PRICED, STALE}


class PricingError(Exception):
    """Erro de precificação."""


class PricingBlocked(PricingError):
    """Produto/fornecedor sem condições de ser precificado (código estável)."""


class PricingConflict(PricingError):
    """Conflito estrutural de precificação (ex.: mais de um fornecedor ativo)."""


@dataclass
class PricingResult:
    landed_cost: Decimal
    target_margin_percent: Decimal
    sale_price: Decimal
    margin_percent: Decimal
    markup_percent: Decimal


# --------------------------------------------------------------------------
# helpers puros (Decimal; nunca float)
# --------------------------------------------------------------------------

def to_decimal(value) -> Optional[Decimal]:
    """Normaliza entrada para Decimal. Nenhuma operação usa float."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _ratio_percent(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator * Decimal("100")).quantize(Decimal("0.01"))


def compute_margin(sale_price, landed_cost) -> Decimal:
    """margem (%) = (preço_venda - landed_cost) / preço_venda * 100

    Ambos os operandos são normalizados via to_decimal() (contrato Fase 6).
    """
    sale = to_decimal(sale_price)
    if sale is None or sale <= 0:
        raise ValueError("sale_price inválido para margem")
    landed = to_decimal(landed_cost)
    if landed is None:
        raise ValueError("landed_cost inválido para margem")
    return _ratio_percent(sale - landed, sale)


def compute_markup(sale_price, landed_cost) -> Decimal:
    """markup (%) = (preço_venda - landed_cost) / landed_cost * 100

    Ambos os operandos são normalizados via to_decimal() (contrato Fase 6).
    """
    sale = to_decimal(sale_price)
    if sale is None:
        raise ValueError("sale_price inválido para markup")
    landed = to_decimal(landed_cost)
    if landed is None or landed <= 0:
        raise ValueError("landed_cost inválido para markup")
    return _ratio_percent(sale - landed, landed)


def sale_price_for_margin(landed_cost, margin_percent) -> Decimal:
    """Preço de venda para margem alvo M: landed / (1 - M/100), arredondado
    para CIMA até o centavo (o arredondamento nunca reduz a margem alvo)."""
    landed = to_decimal(landed_cost)
    if landed is None or landed <= 0:
        raise PricingBlocked("landed_cost_missing")
    margin = to_decimal(margin_percent)
    if margin is None:
        raise PricingBlocked("margin_missing")
    if not (Decimal("0") < margin < Decimal("100")):
        raise PricingBlocked("margin_invalid")
    divisor = Decimal("1") - margin / Decimal("100")
    return (landed / divisor).quantize(Decimal("0.01"), rounding=ROUND_UP)


def normalize_currency(value) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


# --------------------------------------------------------------------------
# landed cost
# --------------------------------------------------------------------------

def calculate_landed_cost(supplier_product: SupplierProduct) -> Decimal:
    """Calcula o landed cost REAL observado: cost + shipping_cost.

    Requisitos (todos):
      cost != NULL e cost > 0
      freight_status == 'available'
      shipping_cost != NULL e >= 0   (0.00 é válido somente com frete real
                                      confirmado, o que exige freight_status
                                      == 'available'; negativo é inválido e é
                                      rejeitado aqui, sem depender do CHECK)
      currency e freight_currency presentes e iguais

    Falha de qualquer requisito: exige supplier_product.landed_cost = None
    (indisponibilidade NUNCA vira zero) e levanta PricingBlocked.
    """
    cost = to_decimal(supplier_product.cost)
    if cost is None or cost <= 0:
        supplier_product.landed_cost = None
        raise PricingBlocked("supplier_cost_missing")

    if supplier_product.freight_status != FREIGHT_AVAILABLE:
        supplier_product.landed_cost = None
        raise PricingBlocked("freight_unavailable")

    shipping = to_decimal(supplier_product.shipping_cost)
    if shipping is None:
        supplier_product.landed_cost = None
        raise PricingBlocked("shipping_cost_missing")
    if shipping < 0:
        supplier_product.landed_cost = None
        raise PricingBlocked("shipping_cost_invalid")

    supplier_currency = normalize_currency(supplier_product.currency)
    freight_currency = normalize_currency(supplier_product.freight_currency)
    if not supplier_currency or not freight_currency:
        supplier_product.landed_cost = None
        raise PricingBlocked("currency_mismatch")
    if supplier_currency != freight_currency:
        supplier_product.landed_cost = None
        raise PricingBlocked("currency_mismatch")

    landed = cost + shipping
    return landed.quantize(Decimal("0.01"))


# --------------------------------------------------------------------------
# precificar um draft
# --------------------------------------------------------------------------

def price_dropshipping_product(
    *,
    product: Product,
    target_margin_percent,
) -> PricingResult:
    """Precifica um draft AliExpress SEM publicar (pricing != publication).

    Exige exatamente UM SupplierProduct ativo para o produto. Exige também
    coerência total de moeda: Product.currency == SupplierProduct.currency ==
    SupplierProduct.freight_currency (não existe camada FX). Primeiro valida
    fronteiras; depois calcula o landed cost; depois valida a margem alvo
    (>0 e <100); persiste no modelo (o caller decide quando commitar).
    """
    if product.source != SOURCE_ALIEXPRESS:
        raise PricingError("only_aliexpress")

    active_links = [
        sp for sp in (product.supplier_links or []) if sp.is_active
    ]
    if len(active_links) == 0:
        logger.info("pricing: blocked reason=supplier_missing")
        raise PricingBlocked("supplier_missing")
    if len(active_links) > 1:
        logger.info("pricing: blocked reason=supplier_ambiguous")
        raise PricingConflict("supplier_ambiguous")
    supplier_product = active_links[0]

    selling_currency = normalize_currency(product.currency)
    supplier_currency = normalize_currency(supplier_product.currency)
    freight_currency = normalize_currency(supplier_product.freight_currency)
    if (
        not selling_currency
        or selling_currency != supplier_currency
        or not freight_currency
        or supplier_currency != freight_currency
    ):
        # Moeda de VENDA precisa igualar fornecedor×frete; sem isso o preço de
        # venda não passa a valer (nunca marca pricing_status='priced').
        logger.info("pricing: blocked reason=currency_mismatch")
        raise PricingBlocked("currency_mismatch")

    try:
        landed_cost = calculate_landed_cost(supplier_product)

        margin = to_decimal(target_margin_percent)
        if margin is None:
            raise PricingBlocked("margin_missing")
        if not (Decimal("0") < margin < Decimal("100")):
            raise PricingBlocked("margin_invalid")

        sale_price = sale_price_for_margin(landed_cost, margin)
    except PricingBlocked as exc:
        code = exc.args[0] if exc.args else "unknown"
        logger.info("pricing: blocked reason=%s", code)
        raise

    supplier_product.landed_cost = landed_cost
    product.target_margin_percent = margin
    product.price = sale_price
    product.pricing_status = PRICED
    product.pricing_error = None
    product.pricing_updated_at = utcnow()
    logger.info("pricing: success")
    return PricingResult(
        landed_cost=landed_cost,
        target_margin_percent=margin,
        sale_price=sale_price,
        margin_percent=compute_margin(sale_price, landed_cost),
        markup_percent=compute_markup(sale_price, landed_cost),
    )


__all__ = [
    "PricingError",
    "PricingBlocked",
    "PricingConflict",
    "PricingResult",
    "SOURCE_ALIEXPRESS",
    "FREIGHT_AVAILABLE",
    "UNPRICED",
    "BLOCKED",
    "READY",
    "PRICED",
    "STALE",
    "PRICING_STATUSES",
    "to_decimal",
    "compute_margin",
    "compute_markup",
    "sale_price_for_margin",
    "normalize_currency",
    "calculate_landed_cost",
    "price_dropshipping_product",
]