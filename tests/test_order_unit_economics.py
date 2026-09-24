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



def _snapshot_item(quantity="1", **overrides):
    values = {
        "quantity": quantity,
        "supplier_unit_cost_snapshot": Decimal("30.00"),
        "supplier_shipping_cost_snapshot": Decimal("5.00"),
        "supplier_landed_cost_snapshot": Decimal("35.00"),
        "supplier_cost_currency_snapshot": "BRL",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_snapshot_adapter_aggregates_landed_cost_by_quantity():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_from_snapshot,
        resolve_order_landed_cost_snapshot,
    )

    order = _order(
        subtotal=Decimal("150.00"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("150.00"),
        items=[_snapshot_item(quantity=2), _snapshot_item()],
    )

    assert resolve_order_landed_cost_snapshot(order) == Decimal("105.00")
    result = evaluate_order_unit_economics_from_snapshot(
        order,
        policy=_complete_policy(),
    )
    assert result.complete is True
    assert result.evaluation is not None
    assert result.evaluation.landed_cost == Decimal("105.00")


def test_snapshot_adapter_keeps_legacy_order_incomplete():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_from_snapshot,
    )

    order = _order(items=[
        _snapshot_item(supplier_landed_cost_snapshot=None)
    ])
    result = evaluate_order_unit_economics_from_snapshot(
        order,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert result.unknown_components == ("landed_cost",)


@pytest.mark.parametrize(
    "item,error",
    [
        (_snapshot_item(quantity=0), "order_item_quantity_invalid"),
        (
            _snapshot_item(supplier_landed_cost_snapshot="99.00"),
            "order_item_landed_cost_mismatch",
        ),
        (
            _snapshot_item(supplier_cost_currency_snapshot="USD"),
            None,
        ),
    ],
)
def test_snapshot_integrity_validation(item, error):
    from loja.services.order_unit_economics import (
        resolve_order_landed_cost_snapshot,
    )

    items = [item]
    if error is None:
        items.append(_snapshot_item())

    with pytest.raises(OrderUnitEconomicsError, match=(
        error or "order_item_cost_currency_mismatch"
    )):
        resolve_order_landed_cost_snapshot(_order(items=items))


def test_snapshot_adapter_does_not_mutate_order_or_items():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_from_snapshot,
    )

    item = _snapshot_item()
    order = _order(items=[item])
    before_order = dict(vars(order))
    before_item = dict(vars(item))

    evaluate_order_unit_economics_from_snapshot(
        order,
        policy=_complete_policy(),
    )

    assert vars(order) == before_order
    assert vars(item) == before_item



def _approved_payment(order, *, net_received="84.00", **overrides):
    values = {
        "order_id": getattr(order, "id", None),
        "provider": "mercado_pago",
        "status": "approved",
        "amount": order.total,
        "currency": "BRL",
        "raw_payload": {
            "transaction_amount": str(order.total),
            "currency_id": "BRL",
            "transaction_details": {
                "net_received_amount": net_received,
            },
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_observed_processor_retention_replaces_gateway_estimates():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(
        id="order-1",
        items=[_snapshot_item()],
    )
    payment = _approved_payment(order, net_received="84.00")

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is True
    assert result.evaluation is not None
    # Retenção observada: 90 - 84 = 6; desconto: 10; imposto: 0.
    # A estimativa de gateway (4,50) não é somada.
    assert result.evaluation.known_variable_costs == Decimal("16.00")
    assert "gateway_fee" not in result.unknown_components
    assert "installment_cost" not in result.unknown_components


def test_missing_payment_evidence_fails_closed_not_additive():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(
        order,
        raw_payload={},
    )

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert result.evaluation is not None
    assert result.evaluation.operational_margin is None
    assert "gateway_fee" in result.unknown_components
    assert "installment_cost" in result.unknown_components
    assert "payment_fee" not in result.unknown_components


def test_payment_none_fails_closed_without_additive():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    result = evaluate_order_unit_economics_with_payment(
        order,
        None,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert "gateway_fee" in result.unknown_components
    assert "installment_cost" in result.unknown_components


def test_payment_scenario_cash_uses_gateway_total_fee():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(order, raw_payload={"installments": 1})

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is True
    assert result.evaluation is not None
    # 90 * 5% = 4.50 (único payment_fee) + desconto 10 + imposto 0
    assert result.evaluation.known_variable_costs == Decimal("14.50")
    assert "gateway_fee" not in result.unknown_components
    assert "installment_cost" not in result.unknown_components


def test_payment_scenario_installment_uses_installment_total_fee():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(order, raw_payload={"installments": 6})

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is True
    assert result.evaluation is not None
    # 90 * 0% = 0 (único payment_fee) + desconto 10 + imposto 0
    assert result.evaluation.known_variable_costs == Decimal("10.00")


def test_observed_retention_removes_gateway_unknowns_only():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(order)

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=load_unit_economics_cost_policy({}),
    )

    assert "gateway_fee" not in result.unknown_components
    assert "installment_cost" not in result.unknown_components
    assert result.unknown_components == (
        "tax",
        "acquisition_cost",
        "refund_reserve",
    )


def test_payment_must_match_order_and_charged_amount():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])

    with pytest.raises(OrderUnitEconomicsError, match="payment_order_mismatch"):
        evaluate_order_unit_economics_with_payment(
            order,
            _approved_payment(order, order_id="order-2"),
            policy=_complete_policy(),
        )

    with pytest.raises(
        OrderUnitEconomicsError,
        match="payment_charged_amount_mismatch",
    ):
        evaluate_order_unit_economics_with_payment(
            order,
            _approved_payment(
                order,
                amount="80.00",
                raw_payload={
                    "transaction_amount": "80.00",
                    "currency_id": "BRL",
                    "transaction_details": {
                        "net_received_amount": "75.00",
                    },
                },
            ),
            policy=_complete_policy(),
        )


def test_amount_mismatch_without_retention_is_rejected():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(order, amount="80.00", raw_payload={})

    with pytest.raises(
        OrderUnitEconomicsError,
        match="payment_charged_amount_mismatch",
    ):
        evaluate_order_unit_economics_with_payment(
            order,
            payment,
            policy=_complete_policy(),
        )


def test_payload_transaction_amount_mismatch_without_net_is_rejected():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(
        order,
        raw_payload={"transaction_amount": "80.00", "currency_id": "BRL"},
    )

    with pytest.raises(
        OrderUnitEconomicsError,
        match="payment_payload_amount_mismatch",
    ):
        evaluate_order_unit_economics_with_payment(
            order,
            payment,
            policy=_complete_policy(),
        )


def test_payload_currency_mismatch_is_rejected():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(
        order,
        raw_payload={
            "transaction_amount": str(order.total),
            "currency_id": "USD",
            "installments": 1,
        },
    )

    with pytest.raises(
        OrderUnitEconomicsError,
        match="payment_payload_currency_mismatch",
    ):
        evaluate_order_unit_economics_with_payment(
            order,
            payment,
            policy=_complete_policy(),
        )


def test_pending_payment_is_incomplete_not_complete():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(
        order,
        status="pending",
        raw_payload={"installments": 1},
    )

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert result.evaluation is not None
    assert result.evaluation.operational_margin is None
    assert "gateway_fee" in result.unknown_components


def test_non_mercadopago_provider_is_incomplete():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(
        order,
        provider="other",
        raw_payload={"installments": 1},
    )

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert "gateway_fee" in result.unknown_components


def test_missing_payment_amount_is_rejected():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(order, amount=None, raw_payload={})

    with pytest.raises(
        OrderUnitEconomicsError,
        match="payment_amount_missing",
    ):
        evaluate_order_unit_economics_with_payment(
            order,
            payment,
            policy=_complete_policy(),
        )


def test_invalid_modality_evidence_stays_incomplete():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(order, raw_payload={"installments": "3"})

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is False
    assert "gateway_fee" in result.unknown_components


def test_retention_still_priority_over_installments():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    order = _order(id="order-1", items=[_snapshot_item()])
    payment = _approved_payment(
        order,
        net_received="84.00",
        raw_payload={
            "transaction_amount": str(order.total),
            "currency_id": "BRL",
            "installments": 6,
            "transaction_details": {"net_received_amount": "84.00"},
        },
    )

    result = evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert result.complete is True
    assert result.evaluation is not None
    # retenção 6.00 + desconto 10 (não usa taxa de parcelamento)
    assert result.evaluation.known_variable_costs == Decimal("16.00")


def test_with_payment_does_not_mutate_order_or_payment():
    from loja.services.order_unit_economics import (
        evaluate_order_unit_economics_with_payment,
    )

    item = _snapshot_item()
    order = _order(id="order-1", items=[item])
    payment = _approved_payment(order, raw_payload={"installments": 1})
    before_order = dict(vars(order))
    before_payment = dict(vars(payment))
    before_payload = dict(payment.raw_payload)

    evaluate_order_unit_economics_with_payment(
        order,
        payment,
        policy=_complete_policy(),
    )

    assert vars(order) == before_order
    assert vars(payment) == before_payment
    assert payment.raw_payload == before_payload
    assert vars(item) == dict(vars(item))
