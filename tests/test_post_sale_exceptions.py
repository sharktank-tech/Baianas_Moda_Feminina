from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from loja.services.post_sale_exceptions import (
    DEFAULT_STALE_AFTER_HOURS,
    build_post_sale_exception_snapshot,
    load_stale_after_hours,
)


NOW = datetime(2026, 9, 13, 12, 0, 0)


def _fulfillment(
    *,
    state="confirmed",
    age=1,
    refund_state=None,
    refund_age=None,
    tracking="ABC12",
):
    updated = None if age is None else NOW - timedelta(hours=age)
    order = SimpleNamespace(
        order_number="PM-SAFE",
        supplier_refund_state=refund_state,
        supplier_refund_requested_at=(
            None if refund_age is None else NOW - timedelta(hours=refund_age)
        ),
        updated_at=updated,
    )
    return SimpleNamespace(
        id="local-fulfillment-id",
        state=state,
        updated_at=updated,
        order=order,
        supplier=SimpleNamespace(name="Fornecedor"),
        remote_orders=(
            [] if tracking is False
            else [SimpleNamespace(tracking_code=tracking)]
        ),
        ali_order_id="MUST_NOT_LEAK",
        attempt_key="MUST_NOT_LEAK",
        last_error="MUST_NOT_LEAK",
    )


def test_default_stale_window(monkeypatch):
    monkeypatch.delenv("FULFILLMENT_STALE_AFTER_HOURS", raising=False)
    assert load_stale_after_hours() == DEFAULT_STALE_AFTER_HOURS


@pytest.mark.parametrize("value", [" 48", "48 ", "0", "721", "-1", "abc"])
def test_invalid_stale_window_fails_closed(monkeypatch, value):
    monkeypatch.setenv("FULFILLMENT_STALE_AFTER_HOURS", value)
    with pytest.raises(ValueError, match="fulfillment_stale_after_invalid"):
        load_stale_after_hours()


def test_manual_review_is_always_queued():
    snapshot = build_post_sale_exception_snapshot(
        [_fulfillment(state="manual_review")], now=NOW
    )
    assert snapshot.rows[0].reasons == ("manual_review",)


def test_pending_reconciliation_is_always_queued():
    snapshot = build_post_sale_exception_snapshot(
        [_fulfillment(state="pending_reconciliation")], now=NOW
    )
    assert snapshot.rows[0].reasons == ("pending_reconciliation",)


def test_stale_fulfillment_uses_configured_boundary():
    fresh = _fulfillment(state="confirmed", age=47)
    stale = _fulfillment(state="confirmed", age=48)
    snapshot = build_post_sale_exception_snapshot(
        [fresh, stale], stale_after_hours=48, now=NOW
    )
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].reasons == ("fulfillment_stalled",)


def test_shipped_without_tracking_has_both_reasons():
    snapshot = build_post_sale_exception_snapshot(
        [_fulfillment(state="shipped", age=60, tracking=None)], now=NOW
    )
    assert snapshot.rows[0].reasons == (
        "fulfillment_stalled",
        "tracking_missing",
    )


def test_stale_refund_is_queued():
    snapshot = build_post_sale_exception_snapshot(
        [
            _fulfillment(
                state="cancelled",
                refund_state="pending_webhook",
                refund_age=60,
            )
        ],
        now=NOW,
    )
    assert snapshot.rows[0].reasons == ("refund_stalled",)


def test_failed_refund_is_queued_without_age():
    snapshot = build_post_sale_exception_snapshot(
        [_fulfillment(state="cancelled", refund_state="failed")],
        now=NOW,
    )
    assert snapshot.rows[0].reasons == ("refund_failure",)


def test_completed_or_fresh_fulfillment_is_not_queued():
    snapshot = build_post_sale_exception_snapshot(
        [
            _fulfillment(state="delivered", age=100),
            _fulfillment(state="confirmed", age=2),
            _fulfillment(state="cancelled", age=100),
        ],
        now=NOW,
    )
    assert snapshot.total == 0


def test_projection_never_contains_remote_identity_or_diagnostics():
    snapshot = build_post_sale_exception_snapshot(
        [_fulfillment(state="manual_review")], now=NOW
    )
    row = snapshot.rows[0]
    assert not hasattr(row, "ali_order_id")
    assert not hasattr(row, "attempt_key")
    assert not hasattr(row, "last_error")
