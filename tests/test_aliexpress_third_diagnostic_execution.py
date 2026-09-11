"""FASE 8.0.11S — independent final hard-off for the third canary.

No test performs network I/O. The final execution gate is OFF by default and
must block before delegating to the existing third-canary submit boundary.
"""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import third_diagnostic_execution as execution
from loja.integrations.aliexpress.third_diagnostic_execution import (
    AliExpressThirdDiagnosticExecutionHardOff,
    execute_third_diagnostic_canary_order,
    get_third_diagnostic_execution_status,
)


def _order():
    return SimpleNamespace(id="local-order")


def test_final_execution_layer_is_hard_off_by_default(monkeypatch):
    monkeypatch.delenv(
        "ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED", raising=False
    )

    delegated = {"count": 0}

    def _must_not_delegate(*args, **kwargs):
        delegated["count"] += 1
        raise AssertionError("third-canary boundary must not be reached")

    monkeypatch.setattr(execution, "submit_third_diagnostic_canary_order", _must_not_delegate)

    with pytest.raises(AliExpressThirdDiagnosticExecutionHardOff) as exc:
        execute_third_diagnostic_canary_order(_order(), session="session")

    assert str(exc.value) == "third_diagnostic_execution_hard_off"
    assert delegated["count"] == 0


def test_all_older_gates_are_insufficient_without_final_authorization(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "local-order")
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")
    monkeypatch.delenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", raising=False)
    monkeypatch.delenv("ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED", raising=False)
    monkeypatch.delenv(
        "ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED", raising=False
    )

    monkeypatch.setattr(
        execution,
        "submit_third_diagnostic_canary_order",
        lambda *args, **kwargs: pytest.fail(
            "older gates must not bypass the independent S hard-off"
        ),
    )

    with pytest.raises(AliExpressThirdDiagnosticExecutionHardOff):
        execute_third_diagnostic_canary_order(_order(), session="session")


def test_explicit_final_authorization_only_then_delegates_once(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED", "1")
    sentinel = SimpleNamespace(state="submitted")
    calls = []

    def _delegate(ali_order, *, session):
        calls.append((ali_order, session))
        return sentinel

    monkeypatch.setattr(execution, "submit_third_diagnostic_canary_order", _delegate)
    order = _order()

    result = execute_third_diagnostic_canary_order(order, session="session")

    assert result is sentinel
    assert calls == [(order, "session")]


def test_execution_status_is_sanitized(monkeypatch):
    monkeypatch.delenv(
        "ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED", raising=False
    )
    status = get_third_diagnostic_execution_status()

    assert status.enabled is False
    assert status.reason == "third_diagnostic_execution_hard_off"
    assert not hasattr(status, "token")
    assert not hasattr(status, "payload")
    assert not hasattr(status, "attempt_key")
    assert not hasattr(status, "remote_order_id")
