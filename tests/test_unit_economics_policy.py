from decimal import Decimal

import pytest

from loja.services.unit_economics import evaluate_unit_economics
from loja.services.unit_economics_policy import (
    ACQUISITION_COST_PER_ORDER_ENV,
    GATEWAY_FEE_PERCENT_ENV,
    INSTALLMENT_COST_PERCENT_ENV,
    REFUND_RESERVE_PERCENT_ENV,
    TAX_PERCENT_ENV,
    UnitEconomicsPolicyError,
    build_unit_economics_components,
    load_unit_economics_cost_policy,
)


def _complete_env(**overrides):
    env = {
        GATEWAY_FEE_PERCENT_ENV: "5.00",
        INSTALLMENT_COST_PERCENT_ENV: "0.00",
        TAX_PERCENT_ENV: "0.00",
        ACQUISITION_COST_PER_ORDER_ENV: "7.50",
        REFUND_RESERVE_PERCENT_ENV: "1.00",
    }
    env.update(overrides)
    return env


def test_absent_configuration_remains_unknown():
    policy = load_unit_economics_cost_policy({})
    assert policy.complete is False

    variable, operational = build_unit_economics_components(
        charged_amount="90.00",
        discount_amount="10.00",
        policy=policy,
    )

    by_name = {component.name: component for component in (*variable, *operational)}
    assert by_name["gateway_fee"].status == "unknown"
    assert by_name["installment_cost"].status == "unknown"
    assert by_name["tax"].status == "unknown"
    assert by_name["acquisition_cost"].status == "unknown"
    assert by_name["refund_reserve"].status == "unknown"
    assert by_name["discount"].status == "known"
    assert by_name["discount"].amount == Decimal("10.00")


def test_explicit_zero_is_known_not_unknown():
    policy = load_unit_economics_cost_policy(
        _complete_env(
            **{
                GATEWAY_FEE_PERCENT_ENV: "0",
                ACQUISITION_COST_PER_ORDER_ENV: "0",
            }
        )
    )
    assert policy.complete is True

    variable, operational = build_unit_economics_components(
        charged_amount="100.00",
        discount_amount="0.00",
        policy=policy,
    )
    by_name = {component.name: component for component in (*variable, *operational)}
    assert by_name["gateway_fee"].status == "known"
    assert by_name["gateway_fee"].amount == Decimal("0.00")
    assert by_name["acquisition_cost"].status == "known"
    assert by_name["acquisition_cost"].amount == Decimal("0.00")


def test_percent_costs_use_charged_amount_and_discount_is_counted_once():
    policy = load_unit_economics_cost_policy(
        _complete_env(
            **{
                GATEWAY_FEE_PERCENT_ENV: "10.00",
                ACQUISITION_COST_PER_ORDER_ENV: "5.00",
                REFUND_RESERVE_PERCENT_ENV: "0.00",
            }
        )
    )
    variable, operational = build_unit_economics_components(
        charged_amount="90.00",
        discount_amount="10.00",
        policy=policy,
    )

    result = evaluate_unit_economics(
        # Receita bruta antes do desconto: 100. O cliente pagou 90.
        sale_price="100.00",
        landed_cost="40.00",
        variable_costs=variable,
        operational_costs=operational,
    )

    # gateway = 10% de 90 = 9; desconto = 10; landed = 40.
    assert result.complete is True
    assert result.known_variable_costs == Decimal("19.00")
    assert result.contribution_margin == Decimal("41.00")
    assert result.known_operational_costs == Decimal("5.00")
    assert result.operational_margin == Decimal("36.00")


def test_unknown_policy_keeps_evaluator_fail_closed():
    policy = load_unit_economics_cost_policy({})
    variable, operational = build_unit_economics_components(
        charged_amount="90.00",
        discount_amount="10.00",
        policy=policy,
    )
    result = evaluate_unit_economics(
        sale_price="100.00",
        landed_cost="40.00",
        variable_costs=variable,
        operational_costs=operational,
    )
    assert result.complete is False
    assert result.profitable is False
    assert set(result.unknown_components) == {
        "gateway_fee",
        "installment_cost",
        "tax",
        "acquisition_cost",
        "refund_reserve",
    }


@pytest.mark.parametrize(
    "name,value",
    [
        (GATEWAY_FEE_PERCENT_ENV, "-1"),
        (GATEWAY_FEE_PERCENT_ENV, "100.01"),
        (TAX_PERCENT_ENV, "abc"),
        (REFUND_RESERVE_PERCENT_ENV, "NaN"),
        (ACQUISITION_COST_PER_ORDER_ENV, "-0.01"),
    ],
)
def test_invalid_policy_configuration_fails_closed(name, value):
    env = _complete_env(**{name: value})
    with pytest.raises(UnitEconomicsPolicyError):
        load_unit_economics_cost_policy(env)


@pytest.mark.parametrize(
    "charged,discount",
    [
        ("-1", "0"),
        ("100", "-1"),
        ("NaN", "0"),
        ("100", "Infinity"),
    ],
)
def test_invalid_order_facts_fail_closed(charged, discount):
    policy = load_unit_economics_cost_policy(_complete_env())
    with pytest.raises(UnitEconomicsPolicyError):
        build_unit_economics_components(
            charged_amount=charged,
            discount_amount=discount,
            policy=policy,
        )
