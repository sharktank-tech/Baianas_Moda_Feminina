from decimal import Decimal
from types import SimpleNamespace

import pytest

from loja.services.payment_scenario import (
    resolve_payment_scenario,
    select_order_payment,
)
from loja.services.unit_economics_policy import (
    PAYMENT_SCENARIO_CASH,
    PAYMENT_SCENARIO_INSTALLMENT,
    PAYMENT_SCENARIOS,
    build_unit_economics_components,
    load_unit_economics_cost_policy,
)


def _payment(**overrides):
    values = {
        "provider": "mercado_pago",
        "status": "approved",
        "raw_payload": {"installments": 1},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_scenario_constants_lock_the_documented_contract():
    assert PAYMENT_SCENARIO_CASH == "cash"
    assert PAYMENT_SCENARIO_INSTALLMENT == "installment"
    assert PAYMENT_SCENARIOS == ("cash", "installment")


def test_single_installment_resolves_cash():
    assert resolve_payment_scenario(_payment()) == "cash"


def test_multiple_installments_resolve_installment():
    payment = _payment(raw_payload={"installments": 3})
    assert resolve_payment_scenario(payment) == "installment"


@pytest.mark.parametrize(
    "raw_payload",
    [
        {},
        {"installments": None},
        {"installments": 0},
        {"installments": -1},
        {"installments": True},
        {"installments": False},
        {"installments": "3"},
        {"installments": 1.5},
        {"installments": 2.0},
    ],
)
def test_absent_or_non_integer_evidence_remains_unknown(raw_payload):
    assert resolve_payment_scenario(_payment(raw_payload=raw_payload)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "other"},
        {"provider": None},
        {"provider": ""},
        {"raw_payload": None},
        {"raw_payload": "not-a-dict"},
        {"raw_payload": []},
    ],
)
def test_non_mercadopago_or_missing_payload_is_unknown(overrides):
    assert resolve_payment_scenario(_payment(**overrides)) is None


def test_status_does_not_restrict_modality_evidence():
    payment = _payment(status="pending", raw_payload={"installments": 2})
    assert resolve_payment_scenario(payment) == "installment"


def test_resolver_does_not_mutate_payment_or_payload():
    payment = _payment(raw_payload={"installments": 6})
    before = dict(vars(payment))
    before_payload = dict(payment.raw_payload)

    resolve_payment_scenario(payment)

    assert vars(payment) == before
    assert payment.raw_payload == before_payload


def _confirmed_policy():
    return load_unit_economics_cost_policy(
        {
            "UNIT_ECONOMICS_GATEWAY_FEE_PERCENT": "4.98",
            "UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT": "10",
            "UNIT_ECONOMICS_TAX_PERCENT": "5",
            "UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER": "3.98",
            "UNIT_ECONOMICS_REFUND_RESERVE_PERCENT": "3.98",
        }
    )


def test_resolved_installment_scenario_selects_single_total_fee():
    scenario = resolve_payment_scenario(
        _payment(raw_payload={"installments": 6})
    )

    variable, _ = build_unit_economics_components(
        charged_amount="164.56",
        discount_amount="0",
        policy=_confirmed_policy(),
        payment_scenario=scenario,
    )

    payment_names = [
        component.name
        for component in variable
        if component.name in ("gateway_fee", "installment_cost", "payment_fee")
    ]
    assert payment_names == ["payment_fee"]
    assert variable[0].amount == Decimal("16.46")


def test_resolved_cash_scenario_selects_gateway_total_fee():
    scenario = resolve_payment_scenario(_payment(raw_payload={"installments": 1}))

    variable, _ = build_unit_economics_components(
        charged_amount="164.56",
        discount_amount="0",
        policy=_confirmed_policy(),
        payment_scenario=scenario,
    )

    payment_names = [
        component.name
        for component in variable
        if component.name in ("gateway_fee", "installment_cost", "payment_fee")
    ]
    assert payment_names == ["payment_fee"]
    assert variable[0].amount == Decimal("8.20")


def test_unknown_scenario_preserves_legacy_additive_contract():
    scenario = resolve_payment_scenario(_payment(raw_payload={}))

    assert scenario is None
    variable, _ = build_unit_economics_components(
        charged_amount="164.56",
        discount_amount="0",
        policy=_confirmed_policy(),
        payment_scenario=scenario,
    )

    assert [component.name for component in variable[:2]] == [
        "gateway_fee",
        "installment_cost",
    ]
    assert variable[0].amount == Decimal("8.20")
    assert variable[1].amount == Decimal("16.46")


def _eligible_payment(**overrides):
    from datetime import datetime

    values = {
        "provider": "mercado_pago",
        "status": "approved",
        "amount": Decimal("90.00"),
        "created_at": datetime(2026, 1, 1, 12, 0, 0),
        "raw_payload": {"installments": 1},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _order(total="90.00", payments=None):
    return SimpleNamespace(
        id="order-1",
        total=Decimal(total),
        payments=list(payments or []),
    )


def test_select_order_payment_returns_eligible_approved_payment():
    payment = _eligible_payment()
    selected = select_order_payment(_order(payments=[payment]))
    assert selected is payment


def test_select_order_payment_prefers_newest_created_at():
    from datetime import datetime

    older = _eligible_payment(created_at=datetime(2026, 1, 1, 12, 0, 0))
    newer = _eligible_payment(created_at=datetime(2026, 2, 1, 12, 0, 0))
    selected = select_order_payment(_order(payments=[older, newer]))
    assert selected is newer


def test_select_order_payment_rejects_amount_mismatch():
    payment = _eligible_payment(amount=Decimal("80.00"))
    assert select_order_payment(_order(payments=[payment])) is None


def test_select_order_payment_rejects_non_approved_or_other_provider():
    assert (
        select_order_payment(
            _order(payments=[_eligible_payment(status="pending")])
        )
        is None
    )
    assert (
        select_order_payment(
            _order(payments=[_eligible_payment(provider="other")])
        )
        is None
    )


def test_select_order_payment_none_without_candidates():
    assert select_order_payment(_order(payments=[])) is None
    assert select_order_payment(SimpleNamespace(total=Decimal("90.00"))) is None
    assert (
        select_order_payment(SimpleNamespace(total=None, payments=[])) is None
    )


def test_select_order_payment_does_not_mutate_order_or_payments():
    payment = _eligible_payment()
    order = _order(payments=[payment])
    before_payments = list(order.payments)

    select_order_payment(order)

    assert order.payments == before_payments
    assert order.total == Decimal("90.00")
