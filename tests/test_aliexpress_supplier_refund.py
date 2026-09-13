from decimal import Decimal
from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress.supplier_refund import (
    REFUND_CONFIRMED,
    REFUND_REJECTED,
    REFUND_UNKNOWN,
    SupplierCancellationRefundProvider,
    SupplierCancellationRefundResult,
    evaluate_supplier_cancellation_refund,
    request_supplier_cancellation_refund,
)


class FakeRefundProvider(SupplierCancellationRefundProvider):
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def refund(self, request):
        self.calls.append(request)
        return SupplierCancellationRefundResult(self.outcome)


def _order(
    *,
    order_status="paid",
    fulfillment_states=("cancelled",),
    payment_status="approved",
    amount="100.00",
    total="100.00",
):
    return SimpleNamespace(
        id="local-order-id",
        status=order_status,
        total=Decimal(total),
        aliexpress_orders=[
            SimpleNamespace(state=state) for state in fulfillment_states
        ],
        payments=[
            SimpleNamespace(
                status=payment_status,
                provider_payment_id="payment-id",
                amount=Decimal(amount),
                currency="BRL",
            )
        ],
    )


def test_preflight_accepts_paid_order_with_all_fulfillments_cancelled():
    decision, request = evaluate_supplier_cancellation_refund(_order())
    assert decision.state == "eligible"
    assert request.amount == Decimal("100.00")
    assert request.currency == "BRL"


def test_partial_supplier_cancellation_fails_closed():
    decision, request = evaluate_supplier_cancellation_refund(
        _order(fulfillment_states=("cancelled", "shipped"))
    )
    assert request is None
    assert decision.reason == "refund_requires_all_fulfillments_cancelled"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("pending_payment", "refund_order_state_not_eligible"),
        ("manual_review", "refund_order_state_not_eligible"),
        ("charged_back", "refund_order_state_not_eligible"),
    ],
)
def test_non_paid_order_is_not_eligible(status, reason):
    decision, request = evaluate_supplier_cancellation_refund(
        _order(order_status=status)
    )
    assert request is None
    assert decision.reason == reason


def test_refunded_order_is_idempotently_completed():
    decision, request = evaluate_supplier_cancellation_refund(
        _order(order_status="refunded")
    )
    assert request is None
    assert decision.state == "completed"
    assert decision.reason == "refund_already_confirmed"


def test_payment_amount_mismatch_fails_closed():
    decision, request = evaluate_supplier_cancellation_refund(
        _order(amount="99.99")
    )
    assert request is None
    assert decision.reason == "refund_payment_mismatch"


def test_gate_off_makes_zero_provider_calls(monkeypatch):
    monkeypatch.delenv("SUPPLIER_CANCELLATION_REFUND_ENABLED", raising=False)
    provider = FakeRefundProvider(REFUND_CONFIRMED)
    decision = request_supplier_cancellation_refund(
        _order(), provider=provider
    )
    assert decision.reason == "supplier_cancellation_refund_hard_off"
    assert provider.calls == []


@pytest.mark.parametrize("value", ["0", "true", "01", " 1"])
def test_gate_accepts_only_literal_one(monkeypatch, value):
    monkeypatch.setenv("SUPPLIER_CANCELLATION_REFUND_ENABLED", value)
    provider = FakeRefundProvider(REFUND_CONFIRMED)
    decision = request_supplier_cancellation_refund(
        _order(), provider=provider
    )
    assert decision.state == "blocked"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("outcome", "state", "reason"),
    [
        (REFUND_CONFIRMED, "pending_webhook", "refund_waiting_webhook"),
        (REFUND_REJECTED, "failed", "refund_rejected"),
        (REFUND_UNKNOWN, "pending", "refund_unknown"),
    ],
)
def test_provider_outcomes_do_not_forge_refunded_state(
    monkeypatch, outcome, state, reason
):
    monkeypatch.setenv("SUPPLIER_CANCELLATION_REFUND_ENABLED", "1")
    order = _order()
    provider = FakeRefundProvider(outcome)
    decision = request_supplier_cancellation_refund(
        order, provider=provider
    )
    assert decision.state == state
    assert decision.reason == reason
    assert order.status == "paid"
    assert len(provider.calls) == 1


def test_enabled_gate_still_requires_explicit_provider(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CANCELLATION_REFUND_ENABLED", "1")
    decision = request_supplier_cancellation_refund(_order())
    assert decision.reason == "refund_provider_required"
