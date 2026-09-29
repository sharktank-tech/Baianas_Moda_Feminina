"""FASE 8.2.0-H — margem operacional pré-publicação.

Avaliação pura e explícita para simular a venda de uma unidade antes de publicar.
Não altera Product, não publica, não consulta provider e não usa defaults
financeiros implícitos.

Cenários de pagamento (Pendência 4 da auditoria): a decisão exige a margem
mínima em AMBOS os cenários — à vista (``PAYMENT_SCENARIO_CASH``) e parcelada
(``PAYMENT_SCENARIO_INSTALLMENT``) — com a mesma semântica do painel de
prontidão. O pior cenário alimenta ``evaluation``. O gate continua hard-off
(``UNIT_ECONOMICS_PUBLICATION_GUARD_ENABLED`` só com o literal ``1``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .unit_economics import UnitEconomicsResult, evaluate_unit_economics
from .unit_economics_policy import (
    PAYMENT_SCENARIO_CASH,
    PAYMENT_SCENARIO_INSTALLMENT,
    PAYMENT_SCENARIOS,
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
    if str(getattr(product, "source", "") or "").lower() == "aliexpress":
        from .acquisition_costs import supplier_tax_total, observed_amount, AcquisitionCostUnavailable
        try:
            link = active_links[0]
            expected = observed_amount(getattr(link, "cost", None), positive=True) + observed_amount(getattr(link, "shipping_cost", None)) + supplier_tax_total(link)
            if landed_cost != expected:
                landed_cost = None
        except AcquisitionCostUnavailable:
            landed_cost = None
    if landed_cost is None:
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("landed_cost_missing_or_invalid",),
            unknown_components=(),
            minimum_operational_margin_percent=minimum,
            evaluation=None,
        )

    evaluations: dict[str, UnitEconomicsResult] = {}
    for scenario in PAYMENT_SCENARIOS:
        variable, operational = build_unit_economics_components(
            charged_amount=sale_price,
            discount_amount=Decimal("0.00"),
            policy=policy,
            payment_scenario=scenario,
        )
        evaluations[scenario] = evaluate_unit_economics(
            sale_price=sale_price,
            landed_cost=landed_cost,
            variable_costs=variable,
            operational_costs=operational,
        )

    unknown_union: list[str] = []
    incomplete_evaluation: Optional[UnitEconomicsResult] = None
    for scenario in PAYMENT_SCENARIOS:
        result = evaluations[scenario]
        if not result.complete and incomplete_evaluation is None:
            incomplete_evaluation = result
        for name in result.unknown_components:
            if name not in unknown_union:
                unknown_union.append(name)

    if unknown_union:
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("unit_economics_incomplete",),
            unknown_components=tuple(unknown_union),
            minimum_operational_margin_percent=minimum,
            evaluation=incomplete_evaluation,
        )

    worst = min(
        (evaluations[s] for s in PAYMENT_SCENARIOS),
        key=lambda result: result.operational_margin_percent,
    )
    if (
        worst.operational_margin_percent is None
        or worst.operational_margin_percent < minimum
    ):
        return NetMarginPublicationDecision(
            allowed=False,
            reasons=("operational_margin_below_minimum",),
            unknown_components=(),
            minimum_operational_margin_percent=minimum,
            evaluation=worst,
        )

    return NetMarginPublicationDecision(
        allowed=True,
        reasons=(),
        unknown_components=(),
        minimum_operational_margin_percent=minimum,
        evaluation=worst,
    )


__all__ = [
    "NetMarginPublicationDecision",
    "evaluate_product_net_margin",
]

