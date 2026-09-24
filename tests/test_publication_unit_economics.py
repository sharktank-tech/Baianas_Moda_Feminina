from decimal import Decimal
from types import SimpleNamespace

from loja.services.publication_unit_economics import (
    evaluate_product_net_margin,
)
from loja.services.unit_economics_policy import (
    UnitEconomicsCostPolicy,
    load_unit_economics_cost_policy,
)


def _product(**overrides):
    values = {
        "price": Decimal("150.00"),
        "supplier_links": [
            SimpleNamespace(
                is_active=True,
                landed_cost=Decimal("100.00"),
            )
        ],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _policy():
    return load_unit_economics_cost_policy(
        {
            "UNIT_ECONOMICS_GATEWAY_FEE_PERCENT": "5.00",
            "UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT": "0.00",
            "UNIT_ECONOMICS_TAX_PERCENT": "10.00",
            "UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER": "5.00",
            "UNIT_ECONOMICS_REFUND_RESERVE_PERCENT": "2.00",
        }
    )


def test_allows_product_above_minimum_operational_margin():
    decision = evaluate_product_net_margin(
        _product(),
        policy=_policy(),
        minimum_operational_margin_percent="10.00",
    )

    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.unknown_components == ()
    assert decision.evaluation is not None
    assert decision.evaluation.operational_margin == Decimal("19.50")
    assert decision.evaluation.operational_margin_percent == Decimal("13.00")


def test_blocks_product_below_minimum_operational_margin():
    decision = evaluate_product_net_margin(
        _product(),
        policy=_policy(),
        minimum_operational_margin_percent="15.00",
    )

    assert decision.allowed is False
    assert decision.reasons == ("operational_margin_below_minimum",)
    assert decision.evaluation.operational_margin_percent == Decimal("13.00")


def test_blocks_when_installment_scenario_below_minimum():
    policy = UnitEconomicsCostPolicy(
        gateway_fee_percent=Decimal("5.00"),
        installment_cost_percent=Decimal("30.00"),
        tax_percent=Decimal("10.00"),
        acquisition_cost_per_order=Decimal("5.00"),
        refund_reserve_percent=Decimal("2.00"),
    )
    decision = evaluate_product_net_margin(
        _product(),
        policy=policy,
        minimum_operational_margin_percent="10.00",
    )

    assert decision.allowed is False
    assert decision.reasons == ("operational_margin_below_minimum",)
    assert decision.evaluation is not None
    assert decision.evaluation.operational_margin_percent < Decimal("10.00")
    cash_fee = (Decimal("150.00") * Decimal("5.00") / Decimal("100")).quantize(
        Decimal("0.01")
    )
    installment_fee = (
        Decimal("150.00") * Decimal("30.00") / Decimal("100")
    ).quantize(Decimal("0.01"))
    assert installment_fee > cash_fee


def test_unknown_costs_never_allow_publication():
    decision = evaluate_product_net_margin(
        _product(),
        policy=load_unit_economics_cost_policy({}),
        minimum_operational_margin_percent="10.00",
    )

    assert decision.allowed is False
    assert decision.reasons == ("unit_economics_incomplete",)
    assert decision.unknown_components == (
        "payment_fee",
        "tax",
        "acquisition_cost",
        "refund_reserve",
    )


def test_installment_fee_unknown_keeps_gate_incomplete():
    policy = UnitEconomicsCostPolicy(
        gateway_fee_percent=Decimal("5.00"),
        installment_cost_percent=None,
        tax_percent=Decimal("10.00"),
        acquisition_cost_per_order=Decimal("5.00"),
        refund_reserve_percent=Decimal("2.00"),
    )
    decision = evaluate_product_net_margin(
        _product(),
        policy=policy,
        minimum_operational_margin_percent="10.00",
    )

    assert decision.allowed is False
    assert decision.reasons == ("unit_economics_incomplete",)
    assert decision.unknown_components == ("payment_fee",)


def test_missing_or_invalid_minimum_margin_blocks():
    for value in (None, "", "-0.01", "100", "NaN"):
        decision = evaluate_product_net_margin(
            _product(),
            policy=_policy(),
            minimum_operational_margin_percent=value,
        )
        assert decision.allowed is False
        assert decision.reasons == (
            "minimum_operational_margin_missing_or_invalid",
        )


def test_missing_or_ambiguous_supplier_blocks():
    missing = evaluate_product_net_margin(
        _product(supplier_links=[]),
        policy=_policy(),
        minimum_operational_margin_percent="10",
    )
    ambiguous = evaluate_product_net_margin(
        _product(
            supplier_links=[
                SimpleNamespace(is_active=True, landed_cost="100"),
                SimpleNamespace(is_active=True, landed_cost="90"),
            ]
        ),
        policy=_policy(),
        minimum_operational_margin_percent="10",
    )

    assert missing.reasons == ("supplier_missing",)
    assert ambiguous.reasons == ("supplier_ambiguous",)


def test_evaluator_does_not_mutate_product_or_supplier():
    product = _product()
    supplier = product.supplier_links[0]
    before_product = dict(vars(product))
    before_supplier = dict(vars(supplier))

    evaluate_product_net_margin(
        product,
        policy=_policy(),
        minimum_operational_margin_percent="10",
    )

    assert vars(product) == before_product
    assert vars(supplier) == before_supplier
