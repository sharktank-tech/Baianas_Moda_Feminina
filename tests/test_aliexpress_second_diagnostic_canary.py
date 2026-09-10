"""Safety tests for FASE 8.0.11K second diagnostic AliExpress canary.

No test performs network I/O.  The real provider is never reached; tests use an
injected fake and exercise only fail-closed gate/preflight behavior.
"""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import second_diagnostic_canary as second
from loja.integrations.aliexpress.controlled_canary import CanaryCircuitStatus
from loja.integrations.aliexpress.second_diagnostic_canary import (
    AliExpressSecondDiagnosticCanaryConfigurationError,
    AliExpressSecondDiagnosticCanaryConsumed,
    SecondDiagnosticCanaryPreflight,
    _assert_second_diagnostic_candidate,
    submit_second_diagnostic_canary_order,
)


class _Submitter:
    def submit(self, request):  # pragma: no cover - controlled boundary mocked
        raise AssertionError("provider must not be called directly")


def _order():
    return SimpleNamespace(
        id="local-order",
        state="ready",
        supplier_id="supplier",
        attempt_count=1,
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


def _locally_valid(monkeypatch, prior=None):
    monkeypatch.setattr(second, "_all_attempts", lambda session: [prior or _prior()])
    monkeypatch.setattr(second, "_remote_identity_exists", lambda session, order_id: False)
    monkeypatch.setattr(second, "_preflight_submission_snapshot", lambda session, order_id: None)
    monkeypatch.setattr(second, "_submission_gate", lambda order, items, refresher=None: None)
    monkeypatch.setattr(
        second,
        "get_canary_circuit_status",
        lambda **kwargs: CanaryCircuitStatus(
            open=False,
            blocking_count=0,
            has_in_flight=False,
            has_unknown=False,
        ),
    )


def _arm(monkeypatch, order_id="local-order"):
    monkeypatch.setenv("ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED", "1")
    monkeypatch.delenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", order_id)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")


def test_second_canary_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    with pytest.raises(AliExpressSecondDiagnosticCanaryConfigurationError) as exc:
        submit_second_diagnostic_canary_order(
            _order(),
            submitter=_Submitter(),
            session=object(),
        )

    assert str(exc.value) == "second_diagnostic_canary_disabled"


def test_second_canary_requires_first_gate_off(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    with pytest.raises(AliExpressSecondDiagnosticCanaryConfigurationError) as exc:
        submit_second_diagnostic_canary_order(
            _order(),
            submitter=_Submitter(),
            session=object(),
        )

    assert str(exc.value) == "single_live_canary_must_be_disabled"


def test_second_canary_requires_exactly_one_prior_attempt(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)

    monkeypatch.setattr(second, "_all_attempts", lambda session: [])
    assert second._candidate_reason(order, session=object()) == (
        "second_diagnostic_canary_requires_prior_attempt"
    )

    monkeypatch.setattr(second, "_all_attempts", lambda session: [_prior(), _prior()])
    with pytest.raises(AliExpressSecondDiagnosticCanaryConsumed) as exc:
        _assert_second_diagnostic_candidate(order, session=object())
    assert str(exc.value) == "second_diagnostic_canary_already_consumed"


def test_prior_attempt_must_be_same_order_and_reconciled(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch, _prior(order_id="different"))
    _arm(monkeypatch, order.id)
    assert second._candidate_reason(order, session=object()) == (
        "second_diagnostic_canary_prior_attempt_order_mismatch"
    )

    _locally_valid(monkeypatch, _prior(state="unknown", completed=False))
    assert second._candidate_reason(order, session=object()) == (
        "second_diagnostic_canary_prior_attempt_not_reconciled"
    )


def test_candidate_requires_attempt_count_one(monkeypatch):
    order = _order()
    order.attempt_count = 2
    _locally_valid(monkeypatch)
    _arm(monkeypatch, order.id)

    assert second._candidate_reason(order, session=object()) == (
        "second_diagnostic_canary_attempt_count_mismatch"
    )


def test_candidate_requires_exact_target(monkeypatch):
    order = _order()
    _locally_valid(monkeypatch)
    _arm(monkeypatch, "different-order")

    assert second._candidate_reason(order, session=object()) == (
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

    monkeypatch.setattr(second, "submit_controlled_canary_order", _controlled)

    result = submit_second_diagnostic_canary_order(
        order,
        submitter=submitter,
        session="session",
    )

    assert result is sentinel
    assert calls == [
        (order, submitter, "session", _assert_second_diagnostic_candidate)
    ]


def test_sanitized_preflight_has_no_sensitive_fields():
    status = SecondDiagnosticCanaryPreflight(
        eligible=False,
        reason="second_diagnostic_canary_disabled",
        prior_attempt_count=1,
        circuit_open=False,
        second_gate_enabled=False,
        first_gate_disabled=True,
        controlled_gate_enabled=False,
        target_matches=False,
        placeorder_enabled=False,
    )

    assert not hasattr(status, "attempt_key")
    assert not hasattr(status, "remote_order_id")
    assert not hasattr(status, "token")
    assert not hasattr(status, "payload")
