"""Safety tests for FASE 8.0.11O third diagnostic AliExpress canary.

No test performs network I/O. The boundary is OFF by default and there is no
admin route in this phase; tests use an injected fake and exercise only local
fail-closed gate/preflight behavior.
"""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import third_diagnostic_canary as third
from loja.integrations.aliexpress.controlled_canary import CanaryCircuitStatus
from loja.integrations.aliexpress.third_diagnostic_canary import (
    AliExpressThirdDiagnosticCanaryConfigurationError,
    AliExpressThirdDiagnosticCanaryConsumed,
    ThirdDiagnosticCanaryPreflight,
    _assert_third_diagnostic_candidate,
    submit_third_diagnostic_canary_order,
)


class _Submitter:
    def submit(self, request):  # pragma: no cover - controlled boundary mocked
        raise AssertionError("provider must not be called directly")


def _order():
    return SimpleNamespace(
        id="local-order",
        state="ready",
        supplier_id="supplier",
        attempt_count=2,
        ali_order_id=None,
        items=[object()],
    )


def _prior(order_id="local-order", *, state="rejected", completed=True):
    return SimpleNamespace(
        aliexpress_order_id=order_id,
        state=state,
        completed_at=object() if completed else None,
        created_at=object(),
    )


def _locally_valid(monkeypatch, attempts=None):
    monkeypatch.setattr(
        third,
        "_all_attempts",
        lambda session: attempts if attempts is not None else [_prior(), _prior()],
    )
    monkeypatch.setattr(third, "_remote_identity_exists", lambda session, order_id: False)
    monkeypatch.setattr(third, "_preflight_submission_snapshot", lambda session, order_id: None)
    monkeypatch.setattr(third, "_submission_gate", lambda order, items, refresher=None: None)
    monkeypatch.setattr(
        third,
        "get_canary_circuit_status",
        lambda **kwargs: CanaryCircuitStatus(
            open=False,
            blocking_count=0,
            has_in_flight=False,
            has_unknown=False,
        ),
    )


def _arm(monkeypatch, order_id="local-order"):
    monkeypatch.setenv("ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED", "1")
    monkeypatch.delenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", raising=False)
    monkeypatch.delenv("ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", order_id)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")


def test_third_canary_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    with pytest.raises(AliExpressThirdDiagnosticCanaryConfigurationError) as exc:
        submit_third_diagnostic_canary_order(
            _order(), submitter=_Submitter(), session=object()
        )

    assert str(exc.value) == "third_diagnostic_canary_disabled"


def test_third_canary_requires_first_and_second_gates_off(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    with pytest.raises(AliExpressThirdDiagnosticCanaryConfigurationError) as exc:
        submit_third_diagnostic_canary_order(
            _order(), submitter=_Submitter(), session=object()
        )
    assert str(exc.value) == "single_live_canary_must_be_disabled"

    monkeypatch.delenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED", "1")
    with pytest.raises(AliExpressThirdDiagnosticCanaryConfigurationError) as exc:
        submit_third_diagnostic_canary_order(
            _order(), submitter=_Submitter(), session=object()
        )
    assert str(exc.value) == "second_diagnostic_canary_must_be_disabled"


def test_third_canary_requires_exactly_two_prior_attempts(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch, [_prior()])
    _arm(monkeypatch, order.id)
    assert third._candidate_reason(order, session=object()) == (
        "third_diagnostic_canary_requires_two_prior_attempts"
    )

    _locally_valid(monkeypatch, [_prior(), _prior(), _prior()])
    with pytest.raises(AliExpressThirdDiagnosticCanaryConsumed) as exc:
        _assert_third_diagnostic_candidate(order, session=object())
    assert str(exc.value) == "third_diagnostic_canary_already_consumed"


def test_both_prior_attempts_must_match_order_and_be_reconciled(monkeypatch):
    order = _order()
    _arm(monkeypatch, order.id)

    _locally_valid(monkeypatch, [_prior(), _prior(order_id="different")])
    assert third._candidate_reason(order, session=object()) == (
        "third_diagnostic_canary_prior_attempt_order_mismatch"
    )

    _locally_valid(monkeypatch, [_prior(), _prior(state="unknown", completed=False)])
    assert third._candidate_reason(order, session=object()) == (
        "third_diagnostic_canary_prior_attempts_not_reconciled"
    )


def test_candidate_requires_attempt_count_two(monkeypatch):
    order = _order()
    order.attempt_count = 1
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)
    assert third._candidate_reason(order, session=object()) == (
        "third_diagnostic_canary_attempt_count_mismatch"
    )


def test_candidate_requires_exact_target(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch)
    _arm(monkeypatch, "different-order")
    assert third._candidate_reason(order, session=object()) == (
        "controlled_canary_target_mismatch"
    )


def test_submit_routes_only_through_controlled_boundary(monkeypatch):
    order = _order()
    submitter = _Submitter()
    sentinel = object()
    calls = []
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)

    def _controlled(ali_order, *, submitter, session, pre_submit_guard, **kwargs):
        calls.append((ali_order, submitter, session, pre_submit_guard))
        return sentinel

    monkeypatch.setattr(third, "submit_controlled_canary_order", _controlled)

    result = submit_third_diagnostic_canary_order(
        order, submitter=submitter, session="session"
    )

    assert result is sentinel
    assert calls == [
        (order, submitter, "session", _assert_third_diagnostic_candidate)
    ]


def test_sanitized_preflight_has_no_sensitive_fields():
    status = ThirdDiagnosticCanaryPreflight(
        eligible=False,
        reason="third_diagnostic_canary_disabled",
        prior_attempt_count=2,
        circuit_open=False,
        third_gate_enabled=False,
        first_gate_disabled=True,
        second_gate_disabled=True,
        controlled_gate_enabled=False,
        target_matches=False,
        placeorder_enabled=False,
    )

    assert not hasattr(status, "attempt_key")
    assert not hasattr(status, "remote_order_id")
    assert not hasattr(status, "token")
    assert not hasattr(status, "payload")
