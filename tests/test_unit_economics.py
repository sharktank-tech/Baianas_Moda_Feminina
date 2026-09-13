from decimal import Decimal

import pytest

from loja.services.unit_economics import (
    CostComponent,
    UnitEconomicsError,
    evaluate_unit_economics,
)


def test_complete_unit_economics_calculates_contribution_and_operational_margin():
    result = evaluate_unit_economics(
        sale_price="100.00",
        landed_cost="40.00",
        variable_costs=(
            CostComponent.known("gateway_fee", "5.00"),
            CostComponent.known("tax", "10.00"),
            CostComponent.not_applicable("installment_cost"),
            CostComponent.not_applicable("discount"),
        ),
        operational_costs=(
            CostComponent.known("acquisition_cost", "20.00"),
            CostComponent.known("refund_reserve", "5.00"),
        ),
    )

    assert result.complete is True
    assert result.known_variable_costs == Decimal("15.00")
    assert result.known_operational_costs == Decimal("25.00")
    assert result.contribution_margin == Decimal("45.00")
    assert result.contribution_margin_percent == Decimal("45.00")
    assert result.operational_margin == Decimal("20.00")
    assert result.operational_margin_percent == Decimal("20.00")
    assert result.profitable is True


def test_unknown_cost_never_becomes_zero_or_positive_margin():
    result = evaluate_unit_economics(
        sale_price="100.00",
        landed_cost="40.00",
        variable_costs=(CostComponent.unknown("gateway_fee"),),
        operational_costs=(CostComponent.not_applicable("acquisition_cost"),),
    )

    assert result.complete is False
    assert result.unknown_components == ("gateway_fee",)
    assert result.contribution_margin is None
    assert result.contribution_margin_percent is None
    assert result.operational_margin is None
    assert result.operational_margin_percent is None
    assert result.profitable is False


def test_not_applicable_is_explicit_zero():
    result = evaluate_unit_economics(
        sale_price="50.00",
        landed_cost="30.00",
        variable_costs=(CostComponent.not_applicable("discount"),),
        operational_costs=(),
    )

    assert result.complete is True
    assert result.known_variable_costs == Decimal("0.00")
    assert result.operational_margin == Decimal("20.00")


def test_negative_known_cost_is_rejected():
    with pytest.raises(UnitEconomicsError, match="gateway_fee_negative"):
        CostComponent.known("gateway_fee", "-0.01")


def test_unknown_component_cannot_carry_amount():
    with pytest.raises(UnitEconomicsError, match="tax_unknown_with_amount"):
        evaluate_unit_economics(
            sale_price="100",
            landed_cost="40",
            variable_costs=(
                CostComponent(name="tax", status="unknown", amount=Decimal("1.00")),
            ),
        )


def test_not_applicable_component_cannot_hide_nonzero_cost():
    with pytest.raises(UnitEconomicsError, match="tax_not_applicable_nonzero"):
        evaluate_unit_economics(
            sale_price="100",
            landed_cost="40",
            variable_costs=(
                CostComponent(name="tax", status="not_applicable", amount=Decimal("1.00")),
            ),
        )


def test_duplicate_component_names_are_rejected_across_groups():
    with pytest.raises(UnitEconomicsError, match="component_name_duplicate"):
        evaluate_unit_economics(
            sale_price="100",
            landed_cost="40",
            variable_costs=(CostComponent.known("gateway_fee", "2"),),
            operational_costs=(CostComponent.known("gateway_fee", "3"),),
        )


def test_nonpositive_sale_and_landed_are_rejected():
    with pytest.raises(UnitEconomicsError, match="sale_price_invalid"):
        evaluate_unit_economics(sale_price="0", landed_cost="10")

    with pytest.raises(UnitEconomicsError, match="landed_cost_invalid"):
        evaluate_unit_economics(sale_price="10", landed_cost="0")


def test_complete_negative_margin_is_not_profitable():
    result = evaluate_unit_economics(
        sale_price="50.00",
        landed_cost="40.00",
        variable_costs=(CostComponent.known("gateway_fee", "8.00"),),
        operational_costs=(CostComponent.known("acquisition_cost", "5.00"),),
    )

    assert result.complete is True
    assert result.contribution_margin == Decimal("2.00")
    assert result.operational_margin == Decimal("-3.00")
    assert result.operational_margin_percent == Decimal("-6.00")
    assert result.profitable is False
