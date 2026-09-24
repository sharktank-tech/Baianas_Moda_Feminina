"""FASE 8.2.0-B — política explícita de custos para unit economics.

Este módulo traduz configuração operacional em ``CostComponent`` sem inventar
custos. Ausência de configuração significa ``unknown``; zero só é aceito quando
foi configurado explicitamente como zero.

Contrato contábil para pedidos:
- o evaluator deve receber como ``sale_price`` a receita bruta antes do desconto
  (por exemplo, ``Order.subtotal + Order.shipping``);
- ``discount`` entra como custo variável conhecido exatamente uma vez;
- taxas percentuais desta política usam como base o valor efetivamente cobrado
  (por exemplo, ``Order.total``), porque é a base transacional disponível;
- a política NÃO afirma que essas porcentagens reproduzem a tarifa contratual do
  Mercado Pago: sem configuração explícita, o custo permanece ``unknown``.

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


def _money_fact(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise UnitEconomicsPolicyError(f"{field}_invalid") from None
    if not result.is_finite() or result < 0:
        raise UnitEconomicsPolicyError(f"{field}_invalid")
    return result


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


def _percent_cost(name: str, base_amount: Decimal, percent: Optional[Decimal]) -> CostComponent:
    if percent is None:
        return CostComponent.unknown(name)
    amount = (base_amount * percent / Decimal("100")).quantize(Decimal("0.01"))
    return CostComponent.known(name, amount)


def build_unit_economics_components(
    *,
    charged_amount,
    discount_amount,
    policy: UnitEconomicsCostPolicy,
    processor_retained_amount=None,
    payment_scenario: Optional[str] = None,
) -> tuple[tuple[CostComponent, ...], tuple[CostComponent, ...]]:
    """Converte política + fatos do pedido em componentes canônicos.

    ``charged_amount`` é o valor efetivamente cobrado ao cliente (tipicamente
    ``Order.total``) e serve de base para custos percentuais configurados.
    ``discount_amount`` é o desconto persistido no pedido e vira custo conhecido.
    Quando ``processor_retained_amount`` é informado, o fato observado
    substitui as estimativas de gateway e parcelamento, nunca soma com elas.

    ``payment_scenario`` seleciona "cash" ou "installment": cada taxa é o
    custo total da modalidade, sem soma. None preserva o contrato legado dos
    consumidores de pedidos/publicação. Retenção observada tem precedência.

    IMPORTANTE: ao passar estes componentes para ``evaluate_unit_economics``, o
    ``sale_price`` deve ser a receita bruta ANTES do desconto, por exemplo
    ``Order.subtotal + Order.shipping``. Usar ``Order.total`` como ``sale_price``
    e também incluir ``discount`` causaria dupla contagem.
    """
    charged = _money_fact(charged_amount, "charged_amount")
    discount = _money_fact(discount_amount, "discount_amount")

    if payment_scenario not in (None, "cash", "installment"):
        raise UnitEconomicsPolicyError("payment_scenario_invalid")

    if processor_retained_amount is None and payment_scenario is not None:
        percent = (policy.gateway_fee_percent if payment_scenario == "cash"
                   else policy.installment_cost_percent)
        payment_costs = (_percent_cost("payment_fee", charged, percent),)
    elif processor_retained_amount is None:
        payment_costs = (
            _percent_cost("gateway_fee", charged, policy.gateway_fee_percent),
            _percent_cost(
                "installment_cost",
                charged,
                policy.installment_cost_percent,
            ),
        )
    else:
        retained = _money_fact(
            processor_retained_amount,
            "processor_retained_amount",
        )
        if retained > charged:
            raise UnitEconomicsPolicyError(
                "processor_retained_amount_exceeds_charged"
            )
        # Fato observado substitui ambas as estimativas. A retenção não é
        # decomposta sem evidência do provider.
        payment_costs = (
            CostComponent.known("processor_retention", retained),
        )

    variable = (
        *payment_costs,
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
