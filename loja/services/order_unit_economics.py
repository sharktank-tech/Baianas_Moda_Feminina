"""FASE 8.2.0-C — adapter read-only de unit economics para Order.

Traduz os fatos monetários persistidos em um pedido para o evaluator canônico.
O adapter não consulta catálogo, fornecedor ou provider e não persiste nada.

O landed cost precisa vir de uma fonte canônica congelada pelo caller. Quando
ausente, a avaliação fica incompleta; ele nunca é inferido do catálogo atual.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .unit_economics import UnitEconomicsResult, evaluate_unit_economics
from .unit_economics_policy import (
    UnitEconomicsCostPolicy,
    build_unit_economics_components,
    load_unit_economics_cost_policy,
)


class OrderUnitEconomicsError(ValueError):
    """Fato monetário inválido ou inconsistente no pedido."""


@dataclass(frozen=True)
class OrderUnitEconomicsResult:
    complete: bool
    gross_revenue: Decimal
    charged_amount: Decimal
    discount_amount: Decimal
    unknown_components: tuple[str, ...]
    evaluation: Optional[UnitEconomicsResult]

    @property
    def profitable(self) -> bool:
        return bool(self.evaluation is not None and self.evaluation.profitable)


def _money(value, field: str) -> Decimal:
    if value is None:
        raise OrderUnitEconomicsError(f"{field}_missing")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise OrderUnitEconomicsError(f"{field}_invalid") from None
    if amount < 0:
        raise OrderUnitEconomicsError(f"{field}_negative")
    return amount


def _policy_unknowns(policy: UnitEconomicsCostPolicy) -> tuple[str, ...]:
    fields = (
        ("gateway_fee", policy.gateway_fee_percent),
        ("installment_cost", policy.installment_cost_percent),
        ("tax", policy.tax_percent),
        ("acquisition_cost", policy.acquisition_cost_per_order),
        ("refund_reserve", policy.refund_reserve_percent),
    )
    return tuple(name for name, value in fields if value is None)


def evaluate_order_unit_economics(
    order,
    *,
    landed_cost=None,
    policy: Optional[UnitEconomicsCostPolicy] = None,
) -> OrderUnitEconomicsResult:
    """Avalia um Order sem mutá-lo e sem consultar fontes externas.

    Contrato:
    - receita bruta = subtotal + shipping;
    - valor cobrado = total;
    - desconto é contado exatamente uma vez como custo variável;
    - total deve ser igual a receita bruta - desconto;
    - landed_cost ausente mantém o resultado incompleto e fail-closed.
    """
    subtotal = _money(getattr(order, "subtotal", None), "order_subtotal")
    shipping = _money(getattr(order, "shipping", None), "order_shipping")
    discount = _money(getattr(order, "discount", None), "order_discount")
    charged = _money(getattr(order, "total", None), "order_total")

    gross = (subtotal + shipping).quantize(Decimal("0.01"))
    if discount > gross:
        raise OrderUnitEconomicsError("order_discount_exceeds_gross_revenue")
    if charged != (gross - discount).quantize(Decimal("0.01")):
        raise OrderUnitEconomicsError("order_total_mismatch")

    resolved_policy = policy or load_unit_economics_cost_policy()
    variable, operational = build_unit_economics_components(
        charged_amount=charged,
        discount_amount=discount,
        policy=resolved_policy,
    )

    if landed_cost is None:
        return OrderUnitEconomicsResult(
            complete=False,
            gross_revenue=gross,
            charged_amount=charged,
            discount_amount=discount,
            unknown_components=("landed_cost", *_policy_unknowns(resolved_policy)),
            evaluation=None,
        )

    landed = _money(landed_cost, "landed_cost")
    if landed <= 0:
        raise OrderUnitEconomicsError("landed_cost_invalid")

    evaluation = evaluate_unit_economics(
        sale_price=gross,
        landed_cost=landed,
        variable_costs=variable,
        operational_costs=operational,
    )
    return OrderUnitEconomicsResult(
        complete=evaluation.complete,
        gross_revenue=gross,
        charged_amount=charged,
        discount_amount=discount,
        unknown_components=evaluation.unknown_components,
        evaluation=evaluation,
    )


__all__ = [
    "OrderUnitEconomicsError",
    "OrderUnitEconomicsResult",
    "evaluate_order_unit_economics",
]
