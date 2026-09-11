"""FASE 8.0.11V — independent final hard-off tests. Zero network."""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import fourth_diagnostic_execution as execution
from loja.integrations.aliexpress.fourth_diagnostic_execution import (
    AliExpressFourthDiagnosticExecutionHardOff,
    execute_fourth_diagnostic_canary_order,
    get_fourth_diagnostic_execution_status,
)


def _order():
    return SimpleNamespace(id="local-order")


def test_final_execution_is_hard_off_by_default(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_EXECUTION_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(
        execution,
        "submit_fourth_diagnostic_canary_order",
        lambda *args, **kwargs: calls.append(1),
    )
    with pytest.raises(AliExpressFourthDiagnosticExecutionHardOff) as exc:
        execute_fourth_diagnostic_canary_order(_order(), session="session")
    assert str(exc.value) == "fourth_diagnostic_execution_hard_off"
    assert calls == []


def test_older_and_fourth_boundary_gates_do_not_bypass_final_hard_off(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "local-order")
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")
    monkeypatch.delenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_EXECUTION_ENABLED", raising=False)
    monkeypatch.setattr(
        execution,
        "submit_fourth_diagnostic_canary_order",
        lambda *args, **kwargs: pytest.fail("final hard-off must stop delegation"),
    )
    with pytest.raises(AliExpressFourthDiagnosticExecutionHardOff):
        execute_fourth_diagnostic_canary_order(_order(), session="session")


def test_explicit_final_authorization_delegates_once(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_EXECUTION_ENABLED", "1")
    sentinel = SimpleNamespace(state="submitted")
    calls = []

    def _delegate(ali_order, *, session):
        calls.append((ali_order, session))
        return sentinel

    monkeypatch.setattr(execution, "submit_fourth_diagnostic_canary_order", _delegate)
    order = _order()
    assert execute_fourth_diagnostic_canary_order(order, session="session") is sentinel
    assert calls == [(order, "session")]


def test_execution_status_sanitized(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_FOURTH_DIAGNOSTIC_EXECUTION_ENABLED", raising=False)
    status = get_fourth_diagnostic_execution_status()
    assert status.enabled is False
    assert status.reason == "fourth_diagnostic_execution_hard_off"
    for field in ("token", "payload", "attempt_key", "remote_order_id"):
        assert not hasattr(status, field)
