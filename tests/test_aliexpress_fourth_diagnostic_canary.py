"""FASE 8.0.11V — fourth diagnostic canary safety tests. Zero network."""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import fourth_diagnostic_canary as fourth
from loja.integrations.aliexpress.controlled_canary import CanaryCircuitStatus
from loja.integrations.aliexpress.fourth_diagnostic_canary import (
    AliExpressFourthDiagnosticCanaryConfigurationError,
    AliExpressFourthDiagnosticCanaryConsumed,
    FourthDiagnosticCanaryPreflight,
    _assert_fourth_diagnostic_candidate,
    submit_fourth_diagnostic_canary_order,
)


class _Submitter:
    def submit(self, request):  # pragma: no cover
        raise AssertionError("provider must be reached only through controlled boundary")


def _order():
    return SimpleNamespace(
        id="local-order",
        state="ready",
        supplier_id="supplier",
        attempt_count=3,
        ali_order_id=None,
        items=[object()],
    )


def _prior(order_id="local-order", state="rejected", completed=True):
    return SimpleNamespace(
        aliexpress_order_id=order_id,
        state=state,
        completed_at=object() if completed else None,
        created_at=object(),
    )


def _locally_valid(monkeypatch, attempts=None):
    monkeypatch.setattr(
        fourth,
        "_all_attempts",
        lambda session: attempts if attempts is not None else [_prior(), _prior(), _prior()],
    )
    monkeypatch.setattr(fourth, "_remote_identity_exists", lambda session, order_id: False)
    monkeypatch.setattr(fourth, "_preflight_submission_snapshot", lambda session, order_id: None)
    monkeypatch.setattr(fourth, "_submission_gate", lambda order, items, refresher=None: None)
    monkeypatch.setattr(
        fourth,
        "get_canary_circuit_status",
        lambda **kwargs: CanaryCircuitStatus(
            open=False, blocking_count=0, has_in_flight=False, has_unknown=False
        ),
    )


def _arm(monkeypatch, order_id="local-order"):
    monkeypatch.setenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_CANARY_ENABLED", "1")
    for name in (
        "ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED",
        "ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED",
        "ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED",
        "ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", order_id)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")


def test_fourth_canary_off_by_default(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")
    with pytest.raises(AliExpressFourthDiagnosticCanaryConfigurationError) as exc:
        submit_fourth_diagnostic_canary_order(_order(), submitter=_Submitter(), session=object())
    assert str(exc.value) == "fourth_diagnostic_canary_disabled"


def test_requires_exactly_three_prior_attempts(monkeypatch):
    order = _order()
    _arm(monkeypatch, order.id)
    _locally_valid(monkeypatch, [_prior(), _prior()])
    assert fourth._candidate_reason(order, session=object()) == (
        "fourth_diagnostic_canary_requires_three_prior_attempts"
    )

    _locally_valid(monkeypatch, [_prior(), _prior(), _prior(), _prior()])
    with pytest.raises(AliExpressFourthDiagnosticCanaryConsumed):
        _assert_fourth_diagnostic_candidate(order, session=object())


def test_three_prior_attempts_must_be_rejected_completed_same_order(monkeypatch):
    order = _order()
    _arm(monkeypatch, order.id)
    _locally_valid(monkeypatch, [_prior(), _prior(), _prior(order_id="other")])
    assert fourth._candidate_reason(order, session=object()) == (
        "fourth_diagnostic_canary_prior_attempt_order_mismatch"
    )
    _locally_valid(monkeypatch, [_prior(), _prior(), _prior(state="unknown", completed=False)])
    assert fourth._candidate_reason(order, session=object()) == (
        "fourth_diagnostic_canary_prior_attempts_not_reconciled"
    )


def test_prior_canary_gates_must_be_off(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)
    monkeypatch.setenv("ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED", "1")
    assert fourth._candidate_reason(order, session=object()) == "prior_canary_gates_must_be_disabled"


def test_requires_attempt_count_three_and_exact_target(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)
    order.attempt_count = 2
    assert fourth._candidate_reason(order, session=object()) == "fourth_diagnostic_canary_attempt_count_mismatch"
    order.attempt_count = 3
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "other")
    assert fourth._candidate_reason(order, session=object()) == "controlled_canary_target_mismatch"


def test_submit_delegates_once_through_controlled_boundary(monkeypatch):
    order = _order()
    submitter = _Submitter()
    sentinel = object()
    calls = []
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)

    def _controlled(ali_order, *, submitter, session, pre_submit_guard, **kwargs):
        calls.append((ali_order, submitter, session, pre_submit_guard))
        return sentinel

    monkeypatch.setattr(fourth, "submit_controlled_canary_order", _controlled)
    result = submit_fourth_diagnostic_canary_order(order, submitter=submitter, session="session")
    assert result is sentinel
    assert calls == [(order, submitter, "session", _assert_fourth_diagnostic_candidate)]


def test_preflight_is_sanitized():
    status = FourthDiagnosticCanaryPreflight(
        eligible=False,
        reason="fourth_diagnostic_canary_disabled",
        prior_attempt_count=3,
        circuit_open=False,
        fourth_gate_enabled=False,
        prior_gates_disabled=True,
        controlled_gate_enabled=False,
        target_matches=False,
        placeorder_enabled=False,
    )
    for field in ("token", "payload", "attempt_key", "remote_order_id"):
        assert not hasattr(status, field)
