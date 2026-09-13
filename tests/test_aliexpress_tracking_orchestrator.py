from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress import tracking_orchestrator
from loja.integrations.aliexpress.tracking import (
    AliExpressTrackingObservation,
    AliExpressTrackingPersistenceResult,
    AliExpressTrackingProvider,
    AliExpressTrackingUnavailable,
)


class FakeProvider(AliExpressTrackingProvider):
    def __init__(self, observations=(), error=None):
        self.observations = observations
        self.error = error
        self.calls = []

    def fetch_tracking(self, remote_order_count):
        self.calls.append(remote_order_count)
        if self.error:
            raise self.error("provider failed")
        return self.observations


def _order(state="shipped", count=1):
    return SimpleNamespace(
        state=state,
        remote_orders=[SimpleNamespace(position=i) for i in range(count)],
    )


@pytest.mark.parametrize("value", [None, "", "0", "true", "01", " 1"])
def test_gate_is_strict_and_provider_is_not_called(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", value)
    provider = FakeProvider()
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        _order(), provider=provider
    )
    assert result.reason == "tracking_sync_hard_off"
    assert provider.calls == []


def test_enabled_gate_still_requires_explicit_provider(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(_order())
    assert result.reason == "tracking_provider_required"


def test_ineligible_state_makes_zero_provider_calls(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    provider = FakeProvider()
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        _order(state="ready"), provider=provider
    )
    assert result.reason == "tracking_sync_state_not_eligible"
    assert provider.calls == []


def test_missing_remote_orders_makes_zero_provider_calls(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    provider = FakeProvider()
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        _order(count=0), provider=provider
    )
    assert result.reason == "tracking_remote_orders_missing"
    assert provider.calls == []


def test_happy_path_fetches_once_and_persists(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    provider = FakeProvider(
        (AliExpressTrackingObservation("ABC12", "C1"),)
    )
    captured = []

    def fake_persist(order, snapshot, session=None):
        captured.append((order, snapshot, session))
        return AliExpressTrackingPersistenceResult(1)

    monkeypatch.setattr(
        tracking_orchestrator,
        "persist_sanitized_tracking",
        fake_persist,
    )
    order = _order()
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        order, provider=provider, session="session"
    )
    assert result.state == "completed"
    assert result.persisted == 1
    assert provider.calls == [1]
    assert captured[0][0] is order
    assert captured[0][2] == "session"


def test_pending_tracking_completes_without_inventing_value(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    provider = FakeProvider((AliExpressTrackingObservation(None, None),))
    monkeypatch.setattr(
        tracking_orchestrator,
        "persist_sanitized_tracking",
        lambda *args, **kwargs: AliExpressTrackingPersistenceResult(0),
    )
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        _order(), provider=provider
    )
    assert result.state == "completed"
    assert result.persisted == 0
    assert result.reason == "tracking_pending"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AliExpressTrackingUnavailable, "tracking_provider_unavailable"),
        (RuntimeError, "tracking_provider_error"),
    ],
)
def test_provider_errors_are_sanitized(monkeypatch, error, reason):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    provider = FakeProvider(error=error)
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        _order(), provider=provider
    )
    assert result.state == "failed"
    assert result.reason == reason


def test_provider_and_factory_together_fail_closed(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_SYNC_ENABLED", "1")
    provider = FakeProvider()
    result = tracking_orchestrator.sync_and_persist_aliexpress_tracking(
        _order(), provider=provider, provider_factory=lambda: provider
    )
    assert result.reason == "tracking_provider_ambiguous"
    assert provider.calls == []
