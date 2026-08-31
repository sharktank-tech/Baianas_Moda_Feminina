"""Guarda de publicação de produtos (Fase 6).

Todo caminho de publicação de um produto AliExpress DEVE passar por
publish_product(). A defesa de banco (migration 0005) só garante "AliExpress
ativo => !draft AND priced"; o resto (landed cost, moeda, margem alvo,
variantes) é responsabilidade de evaluate_publication().

Códigos estáveis de bloqueio (NUNCA usar mensagens livres como lógica):
    supplier_missing        -> nenhum SupplierProduct ativo
    supplier_ambiguous      -> mais de um SupplierProduct ativo
    supplier_cost_missing   -> SupplierProduct.cost NULL ou <= 0
    freight_unavailable     -> freight_status != 'available'
    shipping_cost_missing   -> shipping_cost NULL
    shipping_cost_invalid   -> shipping_cost < 0
    landed_cost_missing     -> landed_cost NULL ou <= 0
    landed_cost_stale       -> landed_cost != (cost + shipping) quantizado
    currency_mismatch       -> moeda de venda/fornecedor/frete divergente
    margin_missing          -> target_margin_percent NULL ou inválida
    pricing_not_ready       -> pricing_status != 'priced'
    sale_price_invalid      -> Product.price NULL ou <= 0
    margin_below_target     -> preço abaixo do mínimo para a margem alvo
    no_sellable_variant     -> nenhuma variante do fornecedor ATIVO com estoque
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List

from loja.modules.models import Product

from .pricing import (
    FREIGHT_AVAILABLE,
    PRICED,
    SOURCE_ALIEXPRESS,
    normalize_currency,
    sale_price_for_margin,
    to_decimal,
)

logger = logging.getLogger(__name__)


class PublicationError(Exception):
    """Erro de publicação."""


class PublicationBlocked(PublicationError):
    """Produto AliExpress bloqueado para publicação (razões estáveis)."""

    def __init__(self, reasons: List[str]):
        self.reasons = list(reasons)
        super().__init__("publication_blocked")


@dataclass
class PublicationDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)


def _margin_valid(margin) -> bool:
    return margin is not None and Decimal("0") < margin < Decimal("100")


def evaluate_publication(product: Product) -> PublicationDecision:
    """Avalia se um produto pode ser publicado. Para produtos internos/legados
    (source != 'aliexpress') retorna permitido (fluxo legado preservado)."""
    if product.source != SOURCE_ALIEXPRESS:
        return PublicationDecision(allowed=True, reasons=[])

    reasons: List[str] = []
    active_links = [sp for sp in (product.supplier_links or []) if sp.is_active]
    landed: Decimal | None = None
    selected_supplier = None

    if len(active_links) == 0:
        reasons.append("supplier_missing")
    elif len(active_links) > 1:
        reasons.append("supplier_ambiguous")
    else:
        supplier_product = active_links[0]
        selected_supplier = supplier_product.id

        cost = to_decimal(supplier_product.cost)
        if cost is None or cost <= 0:
            reasons.append("supplier_cost_missing")

        shipping = to_decimal(supplier_product.shipping_cost)
        if supplier_product.freight_status != FREIGHT_AVAILABLE:
            reasons.append("freight_unavailable")
        else:
            if shipping is None:
                reasons.append("shipping_cost_missing")
            elif shipping < 0:
                reasons.append("shipping_cost_invalid")

        landed = to_decimal(supplier_product.landed_cost)
        if landed is None or landed <= 0:
            reasons.append("landed_cost_missing")

        selling_currency = normalize_currency(product.currency)
        supplier_currency = normalize_currency(supplier_product.currency)
        freight_currency = normalize_currency(supplier_product.freight_currency)
        currencies_ok = bool(
            selling_currency
            and supplier_currency == selling_currency
            and freight_currency == supplier_currency
        )
        if not currencies_ok:
            reasons.append("currency_mismatch")

        # Defesa contra landed guardado/inconsistente: qualquer divergência de
        # banco/edição manual entre landed_cost e (cost + shipping) bloqueia.
        # Só avalia quando o estado do fornecedor está íntegro (cost > 0,
        # shipping >= 0, frete disponível e moedas coerentes).
        if (
            cost is not None and cost > 0
            and shipping is not None and shipping >= 0
            and supplier_product.freight_status == FREIGHT_AVAILABLE
            and currencies_ok
        ):
            expected_landed = (cost + shipping).quantize(Decimal("0.01"))
            if landed != expected_landed:
                reasons.append("landed_cost_stale")

    margin = to_decimal(product.target_margin_percent)
    if not _margin_valid(margin):
        reasons.append("margin_missing")

    if product.pricing_status != PRICED:
        reasons.append("pricing_not_ready")

    sale = to_decimal(product.price)
    if sale is None or sale <= 0:
        reasons.append("sale_price_invalid")

    if _margin_valid(margin) and landed is not None and landed > 0 and sale is not None and sale > 0:
        required = sale_price_for_margin(landed, margin)
        if sale < required:
            reasons.append("margin_below_target")

    # O estoque publicável pertence EXCLUSIVAMENTE ao SupplierProduct ativo
    # selecionado: variante ativa de um vínculo de fornecedor inativo não conta.
    has_sellable = False
    if selected_supplier is not None:
        has_sellable = any(
            v.supplier_product_id == selected_supplier
            and getattr(v, "is_active", False)
            and int(v.stock or 0) > 0
            for v in (product.variants or [])
        )
    if not has_sellable:
        reasons.append("no_sellable_variant")

    return PublicationDecision(allowed=not reasons, reasons=reasons)


def publish_product(product: Product) -> None:
    """Publica um produto. Para AliExpress, exige evaluate_publication() ===
    permitido. Nunca finge custo nem recalcula preço escondido durante a
    publicação: o produto precisa estar previamente `priced`."""
    if product.source == SOURCE_ALIEXPRESS:
        decision = evaluate_publication(product)
        if not decision.allowed:
            logger.info(
                "publication: blocked reasons=%s", ",".join(decision.reasons)
            )
            raise PublicationBlocked(decision.reasons)
        product.is_draft = False
        product.is_active = True
        logger.info("publication: success")
        return

    # source != 'aliexpress': preserva o comportamento atual da loja.
    product.is_draft = False
    product.is_active = True
    logger.info("publication: success")


__all__ = [
    "PublicationError",
    "PublicationBlocked",
    "PublicationDecision",
    "evaluate_publication",
    "publish_product",
]