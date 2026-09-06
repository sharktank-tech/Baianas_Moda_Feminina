"""Safety tests for Fase 8.0.11H controlled AliExpress canary.

No test performs network I/O or enables the real placeorder provider.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import controlled_canary as canary
from loja.integrations.aliexpress.controlled_canary import (
    AliExpressCanaryCircuitOpen,
    AliExpressCanaryConfigurationError,
    CanaryCircuitStatus,
    submit_controlled_canary_order,
)
from loja.integrations.aliexpress.reconciliation import _resolve_attempt


class _FakeQuery:
    def __init__(self, result):
        self.result = result

    def filter_by(self, **kwargs):
        return self

    def populate_existing(self):
        return self

    def first(self):
        return self.result


class _FakeSession:
    def __init__(self, order):
        self.order = order

    def query(self, model):
        return _FakeQuery(self.order)


@contextmanager
def _noop_guard(session):
    yield


class _Submitter:
    def submit(self, request):  # pragma: no cover - core submission is mocked
        raise AssertionError("submitter should not be reached directly")


def _arm(monkeypatch, order_id="local-order"):
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", str(order_id))


def test_controlled_canary_requires_explicit_submitter():
    order = SimpleNamespace(id="local-order")
    with pytest.raises(AliExpressCanaryConfigurationError) as exc:
        submit_controlled_canary_order(order, submitter=None, session=object())
    assert str(exc.value) == "controlled_canary_requires_explicit_submitter"


def test_controlled_canary_is_disabled_by_default(monkeypatch):
    order = SimpleNamespace(id="local-order")
    monkeypatch.delenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", raising=False)
    monkeypatch.delenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", raising=False)

    with pytest.raises(AliExpressCanaryConfigurationError) as exc:
        submit_controlled_canary_order(
            order, submitter=_Submitter(), session=object()
        )

    assert str(exc.value) == "controlled_canary_disabled"


def test_controlled_canary_requires_exact_enabled_value(monkeypatch):
    order = SimpleNamespace(id="local-order")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "true")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "local-order")

    with pytest.raises(AliExpressCanaryConfigurationError) as exc:
        submit_controlled_canary_order(
            order, submitter=_Submitter(), session=object()
        )

    assert str(exc.value) == "controlled_canary_disabled"


def test_controlled_canary_requires_exact_target_order(monkeypatch):
    order = SimpleNamespace(id="local-order")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "different-order")

    with pytest.raises(AliExpressCanaryConfigurationError) as exc:
        submit_controlled_canary_order(
            order, submitter=_Submitter(), session=object()
        )

    assert str(exc.value) == "controlled_canary_target_mismatch"
    assert "local-order" not in str(exc.value)
    assert "different-order" not in str(exc.value)


def test_open_global_circuit_blocks_before_core_submission(monkeypatch):
    order = SimpleNamespace(id="local-order")
    session = _FakeSession(order)
    core_calls = []
    _arm(monkeypatch, order.id)

    monkeypatch.setattr(canary, "_global_canary_guard", _noop_guard)
    monkeypatch.setattr(
        canary,
        "get_canary_circuit_status",
        lambda **kwargs: CanaryCircuitStatus(
            open=True,
            blocking_count=1,
            has_in_flight=False,
            has_unknown=True,
        ),
    )
    monkeypatch.setattr(
        canary,
        "submit_aliexpress_order",
        lambda *args, **kwargs: core_calls.append((args, kwargs)),
    )

    with pytest.raises(AliExpressCanaryCircuitOpen) as exc:
        submit_controlled_canary_order(
            order,
            submitter=_Submitter(),
            session=session,
        )

    assert str(exc.value) == "submission_circuit_open"
    assert core_calls == []


def test_closed_circuit_delegates_once_with_explicit_submitter(monkeypatch):
    order = SimpleNamespace(id="local-order")
    session = _FakeSession(order)
    submitter = _Submitter()
    sentinel = object()
    core_calls = []
    _arm(monkeypatch, order.id)

    monkeypatch.setattr(canary, "_global_canary_guard", _noop_guard)
    monkeypatch.setattr(
        canary,
        "get_canary_circuit_status",
        lambda **kwargs: CanaryCircuitStatus(
            open=False,
            blocking_count=0,
            has_in_flight=False,
            has_unknown=False,
        ),
    )

    def _core(ali_order, *, submitter, refresher=None, session=None):
        core_calls.append((ali_order, submitter, refresher, session))
        return sentinel

    monkeypatch.setattr(canary, "submit_aliexpress_order", _core)

    result = submit_controlled_canary_order(
        order,
        submitter=submitter,
        session=session,
    )

    assert result is sentinel
    assert core_calls == [(order, submitter, None, session)]


def test_circuit_error_and_status_do_not_expose_remote_identity():
    remote = "998877665544332211"
    status = CanaryCircuitStatus(
        open=True,
        blocking_count=1,
        has_in_flight=True,
        has_unknown=False,
    )
    error = AliExpressCanaryCircuitOpen("submission_circuit_open")

    assert remote not in repr(status)
    assert remote not in str(error)
    assert not hasattr(status, "attempt_key")
    assert not hasattr(status, "remote_order_id")


class _AttemptQuery:
    def __init__(self, attempt):
        self.attempt = attempt

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self.attempt


class _AttemptSession:
    def __init__(self, attempt):
        self.attempt = attempt

    def query(self, model):
        return _AttemptQuery(self.attempt)


def test_conclusive_reconciliation_can_release_unknown_attempt():
    now = object()
    attempt = SimpleNamespace(
        state="unknown",
        completed_at=None,
        last_error="submission_unknown",
        updated_at=None,
    )
    order = SimpleNamespace(id="local-order", attempt_key="local-attempt-key")
    session = _AttemptSession(attempt)

    _resolve_attempt(order, state="rejected", now=now, session=session)

    assert attempt.state == "rejected"
    assert attempt.completed_at is now
    assert attempt.last_error is None
    assert attempt.updated_at is now


def test_found_reconciliation_marks_attempt_confirmed():
    now = object()
    attempt = SimpleNamespace(
        state="unknown",
        completed_at=None,
        last_error="submission_unknown",
        updated_at=None,
    )
    order = SimpleNamespace(id="local-order", attempt_key="local-attempt-key")
    session = _AttemptSession(attempt)

    _resolve_attempt(order, state="confirmed", now=now, session=session)

    assert attempt.state == "confirmed"
    assert attempt.completed_at is now
    assert attempt.last_error is None
    assert attempt.updated_at is now
