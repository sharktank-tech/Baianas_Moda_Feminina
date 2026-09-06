"""Safety tests for Fase 8.0.11I single live AliExpress canary.

No test performs network I/O.  The real provider factory is never reached
unless explicitly monkeypatched; tests use injected fakes only.
"""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import single_live_canary as live
from loja.integrations.aliexpress.controlled_canary import CanaryCircuitStatus
from loja.integrations.aliexpress.single_live_canary import (
    AliExpressSingleLiveCanaryConfigurationError,
    AliExpressSingleLiveCanaryConsumed,
    SingleLiveCanaryPreflight,
    _assert_single_live_candidate,
    _one_shot_consumed,
    submit_single_live_canary_order,
)


class _FirstQuery:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value

    def filter(self, *args, **kwargs):
        return self


class _FirstSession:
    def __init__(self, value=None):
        self.value = value

    def query(self, model):
        return _FirstQuery(self.value)


class _Submitter:
    def submit(self, request):  # pragma: no cover - boundary mocked
        raise AssertionError("provider must not be called directly")


def _arm_all(monkeypatch, order_id="local-order"):
    monkeypatch.setenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", str(order_id))
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")


def _pristine(monkeypatch):
    monkeypatch.setattr(live, "_one_shot_consumed", lambda session: False)
    monkeypatch.setattr(live, "_remote_identity_exists", lambda session, order_id: False)
    monkeypatch.setattr(live, "_preflight_submission_snapshot", lambda session, order_id: None)
    monkeypatch.setattr(live, "_submission_gate", lambda order, items, refresher=None: None)
    monkeypatch.setattr(
        live,
        "get_canary_circuit_status",
        lambda **kwargs: CanaryCircuitStatus(
            open=False,
            blocking_count=0,
            has_in_flight=False,
            has_unknown=False,
        ),
    )


def test_single_live_canary_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", raising=False)
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")
    order = SimpleNamespace(id="local-order")

    with pytest.raises(AliExpressSingleLiveCanaryConfigurationError) as exc:
        submit_single_live_canary_order(
            order,
            submitter=_Submitter(),
            session=object(),
        )

    assert str(exc.value) == "single_live_canary_disabled"


def test_single_live_canary_requires_placeorder_gate(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", "1")
    monkeypatch.delenv("ALIEXPRESS_PLACEORDER_ENABLED", raising=False)
    order = SimpleNamespace(id="local-order")

    with pytest.raises(AliExpressSingleLiveCanaryConfigurationError) as exc:
        submit_single_live_canary_order(
            order,
            submitter=_Submitter(),
            session=object(),
        )

    assert str(exc.value) == "placeorder_disabled"


def test_any_durable_attempt_consumes_one_shot_regardless_of_state():
    # The query intentionally has no state predicate. confirmed/rejected are
    # therefore just as terminal for the first-canary slot as unknown/in_flight.
    assert _one_shot_consumed(_FirstSession(value=object())) is True
    assert _one_shot_consumed(_FirstSession(value=None)) is False


def test_consumed_one_shot_blocks_before_core(monkeypatch):
    order = SimpleNamespace(
        id="local-order",
        state="ready",
        supplier_id="supplier",
        attempt_count=0,
        ali_order_id=None,
        items=[],
    )
    monkeypatch.setattr(live, "_one_shot_consumed", lambda session: True)

    with pytest.raises(AliExpressSingleLiveCanaryConsumed) as exc:
        _assert_single_live_candidate(order, session=object())

    assert str(exc.value) == "single_live_canary_already_consumed"


def test_pristine_candidate_requires_exact_target(monkeypatch):
    order = SimpleNamespace(
        id="local-order",
        state="ready",
        supplier_id="supplier",
        attempt_count=0,
        ali_order_id=None,
        items=[object()],
    )
    _pristine(monkeypatch)
    monkeypatch.setenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "different-order")
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    assert live._candidate_reason(order, session=object()) == "controlled_canary_target_mismatch"


def test_submit_routes_only_through_controlled_boundary(monkeypatch):
    order = SimpleNamespace(id="local-order")
    submitter = _Submitter()
    sentinel = object()
    calls = []
    _arm_all(monkeypatch, order.id)

    def _controlled(ali_order, *, submitter, session, pre_submit_guard, **kwargs):
        calls.append((ali_order, submitter, session, pre_submit_guard))
        return sentinel

    monkeypatch.setattr(live, "submit_controlled_canary_order", _controlled)

    result = submit_single_live_canary_order(
        order,
        submitter=submitter,
        session="session",
    )

    assert result is sentinel
    assert calls == [(order, submitter, "session", _assert_single_live_candidate)]


def test_sanitized_preflight_has_no_sensitive_identity_fields():
    status = SingleLiveCanaryPreflight(
        eligible=False,
        reason="single_live_canary_already_consumed",
        one_shot_consumed=True,
        circuit_open=False,
        single_gate_enabled=False,
        controlled_gate_enabled=False,
        target_matches=False,
        placeorder_enabled=False,
    )

    assert not hasattr(status, "attempt_key")
    assert not hasattr(status, "remote_order_id")
    assert not hasattr(status, "token")
    assert not hasattr(status, "payload")
