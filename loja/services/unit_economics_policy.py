"""FASE 8.2.0-B — política explícita de custos para unit economics.

Este módulo traduz configuração operacional em ``CostComponent`` sem inventar
custos. Ausência de configuração significa ``unknown``; zero só é aceito quando
foi configurado explicitamente como zero.

Não altera preços, publicação, pedidos, pagamentos ou banco de dados.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from .unit_economics import CostComponent


GATEWAY_FEE_PERCENT_ENV = "UNIT_ECONOMICS_GATEWAY_FEE_PERCENT"
INSTALLMENT_COST_PERCENT_ENV = "UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT"
TAX_PERCENT_ENV = "UNIT_ECONOMICS_TAX_PERCENT"
ACQUISITION_COST_PER_ORDER_ENV = "UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER"
REFUND_RESERVE_PERCENT_ENV = "UNIT_ECONOMICS_REFUND_RESERVE_PERCENT"


class UnitEconomicsPolicyError(ValueError):
    """Configuração inválida de custo operacional."""


@dataclass(frozen=True)
class UnitEconomicsCostPolicy:
    gateway_fee_percent: Optional[Decimal]
    installment_cost_percent: Optional[Decimal]
    tax_percent: Optional[Decimal]
    acquisition_cost_per_order: Optional[Decimal]
    refund_reserve_percent: Optional[Decimal]

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.gateway_fee_percent,
                self.installment_cost_percent,
                self.tax_percent,
                self.acquisition_cost_per_order,
                self.refund_reserve_percent,
            )
        )


def _decimal_config(
    env: Mapping[str, str],
    name: str,
    *,
    maximum: Optional[Decimal] = None,
) -> Optional[Decimal]:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return None

    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise UnitEconomicsPolicyError(f"{name.lower()}_invalid") from None

    if not value.is_finite() or value < 0:
        raise UnitEconomicsPolicyError(f"{name.lower()}_invalid")
    if maximum is not None and value > maximum:
        raise UnitEconomicsPolicyError(f"{name.lower()}_invalid")

    return value.quantize(Decimal("0.01"))


def load_unit_economics_cost_policy(
    env: Optional[Mapping[str, str]] = None,
) -> UnitEconomicsCostPolicy:
    """Carrega política sem defaults financeiros implícitos.

    Campos ausentes permanecem ``None``. Isso é intencional: a camada de unit
    economics converte ``None`` em custo ``unknown`` e não em zero.
    """
    source = env if env is not None else os.environ
    return UnitEconomicsCostPolicy(
        gateway_fee_percent=_decimal_config(
            source, GATEWAY_FEE_PERCENT_ENV, maximum=Decimal("100")
        ),
        installment_cost_percent=_decimal_config(
            source, INSTALLMENT_COST_PERCENT_ENV, maximum=Decimal("100")
        ),
        tax_percent=_decimal_config(
            source, TAX_PERCENT_ENV, maximum=Decimal("100")
        ),
        acquisition_cost_per_order=_decimal_config(
            source, ACQUISITION_COST_PER_ORDER_ENV
        ),
        refund_reserve_percent=_decimal_config(
            source, REFUND_RESERVE_PERCENT_ENV, maximum=Decimal("100")
        ),
    )


def _percent_cost(name: str, base_amount, percent: Optional[Decimal]) -> CostComponent:
    if percent is None:
        return CostComponent.unknown(name)
    base = Decimal(str(base_amount)).quantize(Decimal("0.01"))
    if base < 0:
        raise UnitEconomicsPolicyError(f"{name}_base_invalid")
    amount = (base * percent / Decimal("100")).quantize(Decimal("0.01"))
    return CostComponent.known(name, amount)


def build_unit_economics_components(
    *,
    charged_amount,
    discount_amount,
    policy: UnitEconomicsCostPolicy,
) -> tuple[tuple[CostComponent, ...], tuple[CostComponent, ...]]:
    """Converte política + fatos do pedido em componentes canônicos.

    ``charged_amount`` é o valor efetivamente cobrado ao cliente.
    ``discount_amount`` é o desconto já persistido no pedido; por ser fato
    transacional conhecido, é sempre componente ``known`` (inclusive zero).

    Retorna ``(variable_costs, operational_costs)`` para
    ``evaluate_unit_economics``.
    """
    charged = Decimal(str(charged_amount)).quantize(Decimal("0.01"))
    discount = Decimal(str(discount_amount)).quantize(Decimal("0.01"))
    if charged < 0:
        raise UnitEconomicsPolicyError("charged_amount_invalid")
    if discount < 0:
        raise UnitEconomicsPolicyError("discount_amount_invalid")

    variable = (
        _percent_cost("gateway_fee", charged, policy.gateway_fee_percent),
        _percent_cost(
            "installment_cost", charged, policy.installment_cost_percent
        ),
        CostComponent.known("discount", discount),
        _percent_cost("tax", charged, policy.tax_percent),
    )

    if policy.acquisition_cost_per_order is None:
        acquisition = CostComponent.unknown("acquisition_cost")
    else:
        acquisition = CostComponent.known(
            "acquisition_cost", policy.acquisition_cost_per_order
        )

    operational = (
        acquisition,
        _percent_cost(
            "refund_reserve", charged, policy.refund_reserve_percent
        ),
    )
    return variable, operational


__all__ = [
    "GATEWAY_FEE_PERCENT_ENV",
    "INSTALLMENT_COST_PERCENT_ENV",
    "TAX_PERCENT_ENV",
    "ACQUISITION_COST_PER_ORDER_ENV",
    "REFUND_RESERVE_PERCENT_ENV",
    "UnitEconomicsPolicyError",
    "UnitEconomicsCostPolicy",
    "load_unit_economics_cost_policy",
    "build_unit_economics_components",
]
