import uuid
from decimal import Decimal

from loja import db
from loja.integrations.aliexpress.refund_reconciliation import (
    reconcile_supplier_refund_from_webhook,
    record_supplier_refund_boundary_outcome,
    record_supplier_refund_request,
)
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import AliExpressOrder


def _order(make_user, *, status="paid", fulfillment_state="cancelled"):
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status=status,
        subtotal=Decimal("100.00"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("100.00"),
        customer_name="Cliente",
        customer_email=user.email,
        customer_phone="11999999999",
        zip_code="01001000",
        street="Rua",
        number="1",
        neighborhood="Centro",
        city="Sao Paulo",
        state="SP",
        stock_reserved=False,
    )
    db.session.add(order)
    db.session.flush()
    fulfillment = AliExpressOrder(
        id=uuid.uuid4(),
        order_id=order.id,
        state=fulfillment_state,
    )
    db.session.add(fulfillment)
    db.session.commit()
    return order, fulfillment


def test_records_refund_request_idempotently(db, make_user):
    order, _ = _order(make_user)
    first = record_supplier_refund_request(order)
    requested_at = order.supplier_refund_requested_at
    second = record_supplier_refund_request(order)
    assert first.state == "requested"
    assert first.changed is True
    assert second.state == "requested"
    assert second.changed is False
    assert order.supplier_refund_requested_at == requested_at


def test_request_requires_all_fulfillments_cancelled(db, make_user):
    order, _ = _order(make_user, fulfillment_state="shipped")
    result = record_supplier_refund_request(order)
    assert result.reason == "refund_requires_all_fulfillments_cancelled"
    assert order.supplier_refund_state is None


def test_boundary_confirmed_waits_for_webhook(db, make_user):
    order, _ = _order(make_user)
    record_supplier_refund_request(order)
    result = record_supplier_refund_boundary_outcome(
        order, outcome="confirmed"
    )
    assert result.state == "pending_webhook"
    assert order.status == "paid"
    assert order.supplier_refund_confirmed_at is None


def test_boundary_rejected_is_persisted_without_refunded_status(db, make_user):
    order, _ = _order(make_user)
    record_supplier_refund_request(order)
    result = record_supplier_refund_boundary_outcome(
        order, outcome="rejected"
    )
    assert result.state == "failed"
    assert result.reason == "refund_rejected"
    assert order.status == "paid"
    assert order.supplier_refund_last_error == "refund_rejected"


def test_boundary_unknown_requires_manual_review(db, make_user):
    order, _ = _order(make_user)
    record_supplier_refund_request(order)
    result = record_supplier_refund_boundary_outcome(
        order, outcome="unknown"
    )
    assert result.state == "manual_review"
    assert result.reason == "refund_unknown"
    assert order.status == "paid"


def test_invalid_boundary_outcome_does_not_mutate(db, make_user):
    order, _ = _order(make_user)
    record_supplier_refund_request(order)
    result = record_supplier_refund_boundary_outcome(
        order, outcome="raw token=secret"
    )
    assert result.reason == "refund_outcome_invalid"
    assert order.supplier_refund_state == "requested"


def test_refunded_webhook_confirms_ledger_once(db, make_user):
    order, _ = _order(make_user)
    record_supplier_refund_request(order)
    record_supplier_refund_boundary_outcome(order, outcome="confirmed")
    order.status = "refunded"
    db.session.commit()
    first = reconcile_supplier_refund_from_webhook(order)
    confirmed_at = order.supplier_refund_confirmed_at
    second = reconcile_supplier_refund_from_webhook(order)
    assert first.state == "confirmed"
    assert first.changed is True
    assert second.changed is False
    assert order.supplier_refund_confirmed_at == confirmed_at
    assert order.supplier_refund_last_error is None


def test_unrelated_refund_does_not_create_supplier_ledger(db, make_user):
    order, _ = _order(make_user, status="refunded")
    result = reconcile_supplier_refund_from_webhook(order)
    assert result.reason == "supplier_refund_not_applicable"
    assert order.supplier_refund_state is None
