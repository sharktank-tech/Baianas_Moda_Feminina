import uuid

import pytest

from loja import db
from loja.integrations.aliexpress.supplier_cancellation import (
    CANCEL_CONFIRMED,
    CANCEL_REJECTED,
    CANCEL_UNKNOWN,
    apply_supplier_cancellation_result,
    record_supplier_rejection,
    request_supplier_cancellation,
)
from loja.modules.models import AliExpressOrder


def _order(state):
    order = AliExpressOrder(id=uuid.uuid4(), state=state)
    db.session.add(order)
    db.session.commit()
    return order


@pytest.mark.parametrize("state", ["submitted", "confirmed"])
def test_request_cancellation_from_eligible_state(db, state):
    order = _order(state)
    result = request_supplier_cancellation(order)
    assert result.state == "cancel_requested"
    assert result.changed is True
    assert order.state == "cancel_requested"


def test_request_is_idempotent(db):
    order = _order("cancel_requested")
    result = request_supplier_cancellation(order)
    assert result.state == "cancel_requested"
    assert result.changed is False


@pytest.mark.parametrize(
    "state", ["queued", "ready", "shipped", "delivered", "manual_review"]
)
def test_request_from_ineligible_state_does_not_mutate(db, state):
    order = _order(state)
    result = request_supplier_cancellation(order)
    assert result.reason == "supplier_cancellation_state_not_eligible"
    assert order.state == state


def test_confirmed_cancellation_is_terminal_and_timestamped(db):
    order = _order("cancel_requested")
    result = apply_supplier_cancellation_result(
        order, outcome=CANCEL_CONFIRMED
    )
    assert result.state == "cancelled"
    assert order.cancelled_at is not None
    timestamp = order.cancelled_at
    again = apply_supplier_cancellation_result(
        order, outcome=CANCEL_CONFIRMED
    )
    assert again.changed is False
    assert order.cancelled_at == timestamp


def test_rejected_cancellation_goes_to_manual_review(db):
    order = _order("cancel_requested")
    result = apply_supplier_cancellation_result(
        order, outcome=CANCEL_REJECTED
    )
    assert result.state == "manual_review"
    assert result.reason == "supplier_cancellation_rejected"
    assert order.last_error == "supplier_cancellation_rejected"


def test_unknown_cancellation_remains_requested_without_retry(db):
    order = _order("cancel_requested")
    result = apply_supplier_cancellation_result(
        order, outcome=CANCEL_UNKNOWN
    )
    assert result.state == "cancel_requested"
    assert result.reason == "supplier_cancellation_unknown"
    assert order.last_error == "supplier_cancellation_unknown"


def test_invalid_outcome_does_not_mutate(db):
    order = _order("cancel_requested")
    result = apply_supplier_cancellation_result(
        order, outcome="raw provider message token=secret"
    )
    assert result.reason == "supplier_cancellation_outcome_invalid"
    assert order.state == "cancel_requested"
    assert order.last_error is None


def test_result_without_prior_request_is_blocked(db):
    order = _order("submitted")
    result = apply_supplier_cancellation_result(
        order, outcome=CANCEL_CONFIRMED
    )
    assert result.reason == "supplier_cancellation_not_requested"
    assert order.state == "submitted"


def test_supplier_rejection_uses_fixed_sanitized_reason(db):
    order = _order("submitted")
    result = record_supplier_rejection(order)
    assert result.state == "manual_review"
    assert result.reason == "supplier_rejected"
    assert order.last_error == "supplier_rejected"


@pytest.mark.parametrize("state", ["cancelled", "delivered"])
def test_supplier_rejection_never_reopens_terminal_state(db, state):
    order = _order(state)
    result = record_supplier_rejection(order)
    assert result.reason == "supplier_rejection_terminal_state"
    assert order.state == state
