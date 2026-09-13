from decimal import Decimal
from types import SimpleNamespace

import pytest

from loja.services.order_unit_economics import (
    OrderUnitEconomicsError,
    evaluate_order_unit_economics,
)
from loja.services.unit_economics_policy import load_unit_economics_cost_policy


def _order(**overrides):
    values = {
        "subtotal": Decimal("90.00"),
        "shipping": Decimal("10.00"),
        "discount": Decimal("10.00"),
        "total": Decimal("90.00"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _complete_policy():
    return load_unit_economics_cost_policy(
        {
            "UNIT_ECONOMICS_GATEWAY_FEE_PERCENT": "5.00",
            "UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT": "0.00",
            "UNIT_ECONOMICS_TAX_PERCENT": "0.00",
            "UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER": "7.50",
            "UNIT_ECONOMICS_REFUND_RESERVE_PERCENT": "1.00",
        }
    )


def test_order_adapter_uses_persisted_totals_without_double_counting_discount():
    result = evaluate_order_unit_economics(
        _order(),
        landed_cost="40.00",
        policy=_complete_policy(),
    )

    assert result.complete is True
    assert result.gross_revenue == Decimal("100.00")
    assert result.charged_amount == Decimal("90.00")
    assert result.discount_amount == Decimal("10.00")
    assert result.unknown_components == ()
    assert result.evaluation is not None
    assert result.evaluation.known_variable_costs == Decimal("14.50")
    assert result.evaluation.known_operational_costs == Decimal("8.40")
    assert result.evaluation.contribution_margin == Decimal("45.50")
    assert result.evaluation.operational_margin == Decimal("37.10")
    assert result.profitable is True


def test_missing_landed_cost_is_incomplete_and_never_inferred():
    result = evaluate_order_unit_economics(
        _order(),
        landed_cost=None,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert result.evaluation is None
    assert result.unknown_components == ("landed_cost",)
    assert result.profitable is False


def test_missing_landed_cost_reports_policy_unknowns_too():
    result = evaluate_order_unit_economics(
        _order(),
        landed_cost=None,
        policy=load_unit_economics_cost_policy({}),
    )

    assert result.complete is False
    assert result.unknown_components == (
        "landed_cost",
        "gateway_fee",
        "installment_cost",
        "tax",
        "acquisition_cost",
        "refund_reserve",
    )


def test_unknown_policy_keeps_order_evaluation_fail_closed():
    result = evaluate_order_unit_economics(
        _order(),
        landed_cost="40.00",
        policy=load_unit_economics_cost_policy({}),
    )

    assert result.complete is False
    assert result.evaluation is not None
    assert result.evaluation.operational_margin is None
    assert result.profitable is False


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"total": "89.99"}, "order_total_mismatch"),
        ({"discount": "101.00", "total": "0.00"}, "order_discount_exceeds_gross_revenue"),
        ({"subtotal": None}, "order_subtotal_missing"),
        ({"shipping": "NaN"}, "order_shipping_invalid"),
        ({"discount": "-0.01"}, "order_discount_negative"),
        ({"total": "Infinity"}, "order_total_invalid"),
    ],
)
def test_invalid_or_inconsistent_order_facts_fail_closed(overrides, error):
    with pytest.raises(OrderUnitEconomicsError, match=error):
        evaluate_order_unit_economics(
            _order(**overrides),
            landed_cost="40.00",
            policy=_complete_policy(),
        )


@pytest.mark.parametrize("landed_cost", ["0", "-0.01", "NaN", "Infinity"])
def test_invalid_landed_cost_fails_closed(landed_cost):
    with pytest.raises(OrderUnitEconomicsError, match="landed_cost"):
        evaluate_order_unit_economics(
            _order(),
            landed_cost=landed_cost,
            policy=_complete_policy(),
        )


def test_adapter_does_not_mutate_order():
    order = _order()
    before = dict(vars(order))

    evaluate_order_unit_economics(
        order,
        landed_cost="40.00",
        policy=_complete_policy(),
    )

    assert vars(order) == before
