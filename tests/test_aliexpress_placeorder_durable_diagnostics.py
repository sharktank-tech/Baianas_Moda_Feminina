from __future__ import annotations

from types import SimpleNamespace

from loja.integrations.aliexpress.controlled_canary import _persist_unknown_diagnostic
from loja.integrations.aliexpress.placeorder_diagnostics import (
    clear_unknown_outcome,
    consume_unknown_outcome,
    record_unknown_outcome,
)


class _Query:
    def __init__(self, result):
        self.result = result
        self.filters = None

    def filter_by(self, **kwargs):
        self.filters = kwargs
        return self

    def first(self):
        return self.result


class _Session:
    def __init__(self, attempt):
        self.attempt = attempt
        self.query_obj = None
        self.commits = 0

    def query(self, model):
        self.query_obj = _Query(self.attempt)
        return self.query_obj

    def commit(self):
        self.commits += 1


def test_unknown_outcome_context_is_allowlisted_and_consumed_once():
    clear_unknown_outcome()
    assert record_unknown_outcome("placeorder_transport_error") == "placeorder_transport_error"
    assert consume_unknown_outcome() == "placeorder_transport_error"
    assert consume_unknown_outcome() is None


def test_unknown_outcome_context_rejects_free_form_sensitive_text():
    clear_unknown_outcome()
    assert record_unknown_outcome("token=secret payload=private") == "placeorder_unexpected_error"
    assert consume_unknown_outcome() == "placeorder_unexpected_error"


def test_persist_unknown_diagnostic_updates_only_attempt_last_error():
    clear_unknown_outcome()
    record_unknown_outcome("placeorder_protocol_error")

    attempt = SimpleNamespace(
        state="unknown",
        last_error="submission_unknown",
        updated_at=None,
    )
    order = SimpleNamespace(
        id="local-order",
        attempt_key="local-attempt-key",
        state="pending_reconciliation",
        last_error="submission_unknown",
    )
    session = _Session(attempt)

    _persist_unknown_diagnostic(order, session=session)

    assert attempt.last_error == "placeorder_protocol_error"
    assert attempt.updated_at is not None
    assert order.state == "pending_reconciliation"
    assert order.last_error == "submission_unknown"
    assert session.commits == 1
    assert session.query_obj.filters == {
        "aliexpress_order_id": "local-order",
        "attempt_key": "local-attempt-key",
        "state": "unknown",
    }
    assert consume_unknown_outcome() is None


def test_persist_unknown_diagnostic_without_context_is_noop():
    clear_unknown_outcome()
    attempt = SimpleNamespace(last_error="submission_unknown", updated_at=None)
    order = SimpleNamespace(id="local-order", attempt_key="local-attempt-key")
    session = _Session(attempt)

    _persist_unknown_diagnostic(order, session=session)

    assert attempt.last_error == "submission_unknown"
    assert attempt.updated_at is None
    assert session.commits == 0
    assert session.query_obj is None
