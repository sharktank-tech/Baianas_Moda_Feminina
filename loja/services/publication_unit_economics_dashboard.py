"""FASE 8.2.0 — dashboard administrativo read-only de unit economics.

Somente leitura: não altera Product, não publica, não consulta provider e não
persiste configuração. Reutiliza o avaliador canônico e a política explícita de
custos para expor, por produto: receita bruta, custo (landed), margem de
contribuição e margem líquida operacional por cenário de pagamento (à vista e
parcelada). A margem consolidada usa o pior cenário quando ambos estão
completos; caso contrário permanece ``None``.

Sanitização:
- nunca devolve supplier_sku_id, external ids, tokens ou payload bruto;
- valores ausentes/incompletos permanecem ``None``, nunca zero silencioso;
- a avaliação só fica ``complete`` quando os dois cenários são completos.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Tuple

from loja.modules.models import Product

from .unit_economics import UnitEconomicsResult, evaluate_unit_economics
from .unit_economics_policy import (
    PAYMENT_SCENARIO_CASH,
    PAYMENT_SCENARIO_INSTALLMENT,
    PAYMENT_SCENARIOS,
    UnitEconomicsCostPolicy,
    build_unit_economics_components,
    load_unit_economics_cost_policy,
)


@dataclass(frozen=True)
class DashboardUnitEconomicsRow:
    product_id: str
    product_name: str
    sale_price: Optional[Decimal]
    landed_cost: Optional[Decimal]
    known_variable_costs: Optional[Decimal]
    known_operational_costs: Decimal
    contribution_margin: Optional[Decimal]
    contribution_margin_percent: Optional[Decimal]
    operational_margin: Optional[Decimal]
    operational_margin_percent: Optional[Decimal]
    cash_margin: Optional[Decimal]
    cash_margin_percent: Optional[Decimal]
    installment_margin: Optional[Decimal]
    installment_margin_percent: Optional[Decimal]
    complete: bool
    unknown_components: Tuple[str, ...]
    status: str


def _positive_money(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _status_and_block(
    product: Product,
    sale_price: Optional[Decimal],
    landed_cost: Optional[Decimal],
) -> Tuple[str, str]:
    active_links = [
        link
        for link in (product.supplier_links or [])
        if getattr(link, "is_active", False)
    ]
    if len(active_links) != 1:
        reason = (
            "supplier_missing"
            if not active_links
            else "supplier_ambiguous"
        )
        return reason, reason

    if landed_cost is None:
        return "landed_cost_missing_or_invalid", "landed_cost_missing_or_invalid"

    if sale_price is None:
        return "sale_price_invalid", "sale_price_invalid"

    return "", ""


def build_unit_economics_dashboard(
    products: Iterable[Product],
    policy: Optional[UnitEconomicsCostPolicy] = None,
) -> list[DashboardUnitEconomicsRow]:
    """Avalia unit economics de cada produto de forma read-only e sanitizada."""
    active_policy = policy if policy is not None else load_unit_economics_cost_policy()

    rows: list[DashboardUnitEconomicsRow] = []
    for product in products:
        sale_price = _positive_money(getattr(product, "price", None))

        active_links = [
            link
            for link in (product.supplier_links or [])
            if getattr(link, "is_active", False)
        ]
        landed_cost = (
            _positive_money(active_links[0].landed_cost)
            if len(active_links) == 1
            else None
        )

        status, _ = _status_and_block(product, sale_price, landed_cost)
        if status:
            rows.append(
                DashboardUnitEconomicsRow(
                    product_id=str(product.id),
                    product_name=product.name or "",
                    sale_price=sale_price,
                    landed_cost=landed_cost if status == "landed_cost_missing_or_invalid" else None,
                    known_variable_costs=None,
                    known_operational_costs=Decimal("0.00"),
                    contribution_margin=None,
                    contribution_margin_percent=None,
                    operational_margin=None,
                    operational_margin_percent=None,
                    cash_margin=None,
                    cash_margin_percent=None,
                    installment_margin=None,
                    installment_margin_percent=None,
                    complete=False,
                    unknown_components=(),
                    status=status,
                )
            )
            continue

        evaluations: dict[str, UnitEconomicsResult] = {}
        for scenario in PAYMENT_SCENARIOS:
            variable, operational = build_unit_economics_components(
                charged_amount=sale_price,
                discount_amount=Decimal("0.00"),
                policy=active_policy,
                payment_scenario=scenario,
            )
            evaluations[scenario] = evaluate_unit_economics(
                sale_price=sale_price,
                landed_cost=landed_cost,
                variable_costs=variable,
                operational_costs=operational,
            )

        unknown_union: list[str] = []
        for scenario in PAYMENT_SCENARIOS:
            for name in evaluations[scenario].unknown_components:
                if name not in unknown_union:
                    unknown_union.append(name)

        cash = evaluations[PAYMENT_SCENARIO_CASH]
        installment = evaluations[PAYMENT_SCENARIO_INSTALLMENT]
        both_complete = cash.complete and installment.complete

        if both_complete:
            assert cash.operational_margin_percent is not None
            assert installment.operational_margin_percent is not None
            worst = (
                cash
                if cash.operational_margin_percent
                <= installment.operational_margin_percent
                else installment
            )
        else:
            worst = None

        rows.append(
            DashboardUnitEconomicsRow(
                product_id=str(product.id),
                product_name=product.name or "",
                sale_price=cash.sale_price,
                landed_cost=cash.landed_cost,
                known_variable_costs=(
                    worst.known_variable_costs if worst is not None else None
                ),
                known_operational_costs=cash.known_operational_costs,
                contribution_margin=(
                    worst.contribution_margin if worst is not None else None
                ),
                contribution_margin_percent=(
                    worst.contribution_margin_percent
                    if worst is not None
                    else None
                ),
                operational_margin=(
                    worst.operational_margin if worst is not None else None
                ),
                operational_margin_percent=(
                    worst.operational_margin_percent
                    if worst is not None
                    else None
                ),
                cash_margin=cash.operational_margin,
                cash_margin_percent=cash.operational_margin_percent,
                installment_margin=installment.operational_margin,
                installment_margin_percent=installment.operational_margin_percent,
                complete=both_complete,
                unknown_components=tuple(unknown_union),
                status=(
                    "unit_economics_incomplete"
                    if not both_complete
                    else "ready"
                ),
            )
        )

    return rows


def dashboard_totals(rows: Iterable[DashboardUnitEconomicsRow]) -> dict:
    """Totais agregados read-only para o dashboard (valores somados)."""
    rows_list = list(rows)
    total_revenue = Decimal("0.00")
    total_costs = Decimal("0.00")
    complete = 0
    incomplete = 0
    for row in rows_list:
        if row.sale_price is not None:
            total_revenue += row.sale_price
        if row.landed_cost is not None:
            total_costs += row.landed_cost
        if row.status == "ready":
            complete += 1
        else:
            incomplete += 1
    return {
        "total_products": len(rows_list),
        "complete": complete,
        "incomplete": incomplete,
        "total_revenue": total_revenue.quantize(Decimal("0.01")),
        "total_costs": total_costs.quantize(Decimal("0.01")),
    }


__all__ = [
    "DashboardUnitEconomicsRow",
    "build_unit_economics_dashboard",
    "dashboard_totals",
]