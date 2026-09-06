"""Proves the stricter 8.0.11I guard runs inside H before core submission."""

from contextlib import contextmanager
from types import SimpleNamespace

from loja.integrations.aliexpress import controlled_canary as canary
from loja.integrations.aliexpress.controlled_canary import (
    CanaryCircuitStatus,
    submit_controlled_canary_order,
)


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


class _Submitter:
    def submit(self, request):  # pragma: no cover
        raise AssertionError("provider is mocked at core boundary")


@contextmanager
def _guard(session):
    yield


def test_pre_submit_guard_runs_before_core(monkeypatch):
    order = SimpleNamespace(id="local-order")
    session = _FakeSession(order)
    events = []

    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", "local-order")
    monkeypatch.setattr(canary, "_global_canary_guard", _guard)
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

    def strict_guard(ali_order, *, session):
        events.append(("guard", ali_order, session))

    def core(ali_order, *, submitter, refresher=None, session=None):
        events.append(("core", ali_order, session))
        return "ok"

    monkeypatch.setattr(canary, "submit_aliexpress_order", core)

    result = submit_controlled_canary_order(
        order,
        submitter=_Submitter(),
        session=session,
        pre_submit_guard=strict_guard,
    )

    assert result == "ok"
    assert [event[0] for event in events] == ["guard", "core"]
    assert events[0][1:] == (order, session)
    assert events[1][1:] == (order, session)
