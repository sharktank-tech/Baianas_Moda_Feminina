"""FASE 8.2.0-A — unit economics canônico e fail-closed.

Este módulo é puro/read-only: não consulta provider, não altera Product/Order e
não persiste configuração. Seu objetivo é impedir que custos desconhecidos sejam
silenciosamente tratados como zero ao avaliar rentabilidade.

Semântica:
- ``known``: custo conhecido, valor >= 0 obrigatório;
- ``not_applicable``: componente explicitamente não aplicável, contabiliza 0;
- ``unknown``: custo ainda não conhecido; a avaliação fica INCOMPLETA.

Apenas uma avaliação completa produz ``contribution_margin`` e
``operational_margin`` definitivas.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional


KNOWN = "known"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"
COST_STATUSES = frozenset({KNOWN, NOT_APPLICABLE, UNKNOWN})


class UnitEconomicsError(ValueError):
    """Entrada inválida para a avaliação financeira."""


@dataclass(frozen=True)
class CostComponent:
    name: str
    status: str
    amount: Optional[Decimal] = None

    @classmethod
    def known(cls, name: str, amount) -> "CostComponent":
        return cls(name=name, status=KNOWN, amount=_money(amount, name))

    @classmethod
    def not_applicable(cls, name: str) -> "CostComponent":
        return cls(name=name, status=NOT_APPLICABLE, amount=Decimal("0.00"))

    @classmethod
    def unknown(cls, name: str) -> "CostComponent":
        return cls(name=name, status=UNKNOWN, amount=None)


@dataclass(frozen=True)
class UnitEconomicsResult:
    complete: bool
    sale_price: Decimal
    landed_cost: Decimal
    known_variable_costs: Decimal
    known_operational_costs: Decimal
    unknown_components: tuple[str, ...]
    contribution_margin: Optional[Decimal]
    contribution_margin_percent: Optional[Decimal]
    operational_margin: Optional[Decimal]
    operational_margin_percent: Optional[Decimal]

    @property
    def profitable(self) -> bool:
        """Só afirma lucro quando a avaliação é completa e margem > 0."""
        return bool(
            self.complete
            and self.operational_margin is not None
            and self.operational_margin > 0
        )


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise UnitEconomicsError(f"{field}_invalid") from None
    if not result.is_finite():
        raise UnitEconomicsError(f"{field}_invalid")
    return result


def _money(value, field: str) -> Decimal:
    result = _decimal(value, field)
    if result < 0:
        raise UnitEconomicsError(f"{field}_negative")
    return result.quantize(Decimal("0.01"))


def _percent(amount: Decimal, sale_price: Decimal) -> Decimal:
    return (amount / sale_price * Decimal("100")).quantize(Decimal("0.01"))


def _validate_component(component: CostComponent) -> CostComponent:
    name = str(component.name or "").strip()
    if not name:
        raise UnitEconomicsError("component_name_missing")
    if component.status not in COST_STATUSES:
        raise UnitEconomicsError("component_status_invalid")

    if component.status == KNOWN:
        if component.amount is None:
            raise UnitEconomicsError(f"{name}_amount_missing")
        amount = _money(component.amount, name)
        return CostComponent(name=name, status=KNOWN, amount=amount)

    if component.status == NOT_APPLICABLE:
        if component.amount not in (None, Decimal("0"), Decimal("0.00"), 0, "0", "0.00"):
            raise UnitEconomicsError(f"{name}_not_applicable_nonzero")
        return CostComponent(name=name, status=NOT_APPLICABLE, amount=Decimal("0.00"))

    if component.amount is not None:
        raise UnitEconomicsError(f"{name}_unknown_with_amount")
    return CostComponent(name=name, status=UNKNOWN, amount=None)


def evaluate_unit_economics(
    *,
    sale_price,
    landed_cost,
    variable_costs: Iterable[CostComponent] = (),
    operational_costs: Iterable[CostComponent] = (),
) -> UnitEconomicsResult:
    """Avalia margem de contribuição e margem operacional por unidade.

    ``variable_costs``: custos ligados à transação, por exemplo gateway,
    parcelamento, desconto e imposto.

    ``operational_costs``: custos gerenciais por pedido/unidade, por exemplo CAC
    atribuído e reserva operacional para reembolso/chargeback.

    Se qualquer componente estiver ``unknown``, retornamos apenas os subtotais
    conhecidos. Margens definitivas ficam ``None`` e ``profitable`` é False.
    """
    sale = _money(sale_price, "sale_price")
    if sale <= 0:
        raise UnitEconomicsError("sale_price_invalid")

    landed = _money(landed_cost, "landed_cost")
    if landed <= 0:
        raise UnitEconomicsError("landed_cost_invalid")

    variable = tuple(_validate_component(c) for c in variable_costs)
    operational = tuple(_validate_component(c) for c in operational_costs)

    names = [c.name for c in (*variable, *operational)]
    if len(names) != len(set(names)):
        raise UnitEconomicsError("component_name_duplicate")

    unknown = tuple(c.name for c in (*variable, *operational) if c.status == UNKNOWN)
    known_variable = sum(
        ((c.amount or Decimal("0.00")) for c in variable if c.status != UNKNOWN),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))
    known_operational = sum(
        ((c.amount or Decimal("0.00")) for c in operational if c.status != UNKNOWN),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))

    if unknown:
        return UnitEconomicsResult(
            complete=False,
            sale_price=sale,
            landed_cost=landed,
            known_variable_costs=known_variable,
            known_operational_costs=known_operational,
            unknown_components=unknown,
            contribution_margin=None,
            contribution_margin_percent=None,
            operational_margin=None,
            operational_margin_percent=None,
        )

    contribution = (sale - landed - known_variable).quantize(Decimal("0.01"))
    operational_margin = (contribution - known_operational).quantize(Decimal("0.01"))

    return UnitEconomicsResult(
        complete=True,
        sale_price=sale,
        landed_cost=landed,
        known_variable_costs=known_variable,
        known_operational_costs=known_operational,
        unknown_components=(),
        contribution_margin=contribution,
        contribution_margin_percent=_percent(contribution, sale),
        operational_margin=operational_margin,
        operational_margin_percent=_percent(operational_margin, sale),
    )


__all__ = [
    "KNOWN",
    "NOT_APPLICABLE",
    "UNKNOWN",
    "COST_STATUSES",
    "CostComponent",
    "UnitEconomicsError",
    "UnitEconomicsResult",
    "evaluate_unit_economics",
]
