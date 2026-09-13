"""FASE 8.2.0-H — margem operacional pré-publicação.

Avaliação pura e explícita para simular a venda de uma unidade antes de publicar.
Não altera Product, não publica, não consulta provider e não usa defaults
financeiros implícitos.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .unit_economics import UnitEconomicsResult, evaluate_unit_economics
from .unit_economics_policy import (
    UnitEconomicsCostPolicy,
    build_unit_economics_components,
)


@dataclass(frozen=True)
class NetMarginPublicationDecision:
    allowed: bool
    reasons: tuple[str, ...]
    unknown_components: tuple[str, ...]
    minimum_operational_margin_percent: Optional[Decimal]
    evaluation: Optional[UnitEconomicsResult]


def _positive_money(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _minimum_margin(value) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        margin = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not margin.is_finite() or margin < 0 or margin >= 100:
        return None
    return margin.quantize(Decimal("0.01"))


def evaluate_product_net_margin(
    product,
    *,
    policy: UnitEconomicsCostPolicy,
    minimum_operational_margin_percent,
) -> NetMarginPublicationDecision:
    """Simula uma unidade vendida sem desconto nem frete cobrado ao cliente."""
    minimum = _minimum_margin(minimum_operational_margin_percent)
    if minimum is None:
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("minimum_operational_margin_missing_or_invalid",),
            unknown_components=(),
            minimum_operational_margin_percent=None,
            evaluation=None,
        )

    sale_price = _positive_money(getattr(product, "price", None))
    if sale_price is None:
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("sale_price_invalid",),
            unknown_components=(),
            minimum_operational_margin_percent=minimum,
            evaluation=None,
        )

    active_links = [
        link
        for link in (getattr(product, "supplier_links", None) or [])
        if getattr(link, "is_active", False)
    ]
    if len(active_links) != 1:
        reason = (
            "supplier_missing"
            if not active_links
            else "supplier_ambiguous"
        )
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=(reason,),
            unknown_components=(),
            minimum_operational_margin_percent=minimum,
            evaluation=None,
        )

    landed_cost = _positive_money(
        getattr(active_links[0], "landed_cost", None)
    )
    if landed_cost is None:
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("landed_cost_missing_or_invalid",),
            unknown_components=(),
            minimum_operational_margin_percent=minimum,
            evaluation=None,
        )

    variable, operational = build_unit_economics_components(
        charged_amount=sale_price,
        discount_amount=Decimal("0.00"),
        policy=policy,
    )
    evaluation = evaluate_unit_economics(
        sale_price=sale_price,
        landed_cost=landed_cost,
        variable_costs=variable,
        operational_costs=operational,
    )

    if not evaluation.complete:
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("unit_economics_incomplete",),
            unknown_components=evaluation.unknown_components,
            minimum_operational_margin_percent=minimum,
            evaluation=evaluation,
        )

    if (
        evaluation.operational_margin_percent is None
        or evaluation.operational_margin_percent < minimum
    ):
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("operational_margin_below_minimum",),
            unknown_components=(),
            minimum_operational_margin_percent=minimum,
            evaluation=evaluation,
        )

    return NetMarginPublicationDecision(
        allowed=True,
        reasons=(),
        unknown_components=(),
        minimum_operational_margin_percent=minimum,
        evaluation=evaluation,
    )


__all__ = [
    "NetMarginPublicationDecision",
    "evaluate_product_net_margin",
]
