"""FASE 8.1.0 — política fail-closed do fulfillment de produção."""

import pytest

from loja.integrations.aliexpress.production_fulfillment import (
    PRODUCTION_FULFILLMENT_ENABLED_ENV,
    get_production_order_submitter,
    production_fulfillment_gate_state,
)
from loja.integrations.aliexpress.placeorder_provider import PLACEORDER_ENABLED_ENV
from loja.integrations.aliexpress.submission import HardOffOrderSubmitter


@pytest.mark.parametrize(
    ("production", "placeorder"),
    [
        (None, None),
        ("0", "0"),
        ("1", "0"),
        ("0", "1"),
        ("true", "1"),
        ("1", "true"),
    ],
)
def test_production_provider_selection_fails_closed(
    monkeypatch,
    production,
    placeorder,
):
    if production is None:
        monkeypatch.delenv(PRODUCTION_FULFILLMENT_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(PRODUCTION_FULFILLMENT_ENABLED_ENV, production)

    if placeorder is None:
        monkeypatch.delenv(PLACEORDER_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(PLACEORDER_ENABLED_ENV, placeorder)

    gates = production_fulfillment_gate_state()
    assert gates.can_select_real_provider is False
    assert isinstance(get_production_order_submitter(), HardOffOrderSubmitter)


def test_gate_state_requires_exact_double_one(monkeypatch):
    monkeypatch.setenv(PRODUCTION_FULFILLMENT_ENABLED_ENV, "1")
    monkeypatch.setenv(PLACEORDER_ENABLED_ENV, "1")

    gates = production_fulfillment_gate_state()
    assert gates.production_enabled is True
    assert gates.placeorder_enabled is True
    assert gates.can_select_real_provider is True


def test_double_gate_selects_real_factory_without_submitting(monkeypatch):
    import loja.integrations.aliexpress.production_fulfillment as pf

    sentinel = object()
    calls = []

    monkeypatch.setenv(PRODUCTION_FULFILLMENT_ENABLED_ENV, "1")
    monkeypatch.setenv(PLACEORDER_ENABLED_ENV, "1")

    def fake_factory():
        calls.append("factory")
        return sentinel

    monkeypatch.setattr(pf, "get_real_order_submitter", fake_factory)

    selected = pf.get_production_order_submitter()
    assert selected is sentinel
    assert calls == ["factory"]
