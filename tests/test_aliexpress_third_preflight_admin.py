"""FASE 8.0.11P — admin GET-only preflight for the third AliExpress canary.

No test enables provider HTTP. The phase intentionally exposes no POST submit
route for a third attempt.
"""

from types import SimpleNamespace
import uuid

import pytest

from loja.integrations.aliexpress.third_diagnostic_canary import (
    ThirdDiagnosticCanaryPreflight,
)
from loja.views import aliexpress_single_live_canary_admin as admin_mod


BASE_URL = "/admin/integrations/aliexpress/pedidos"
ORDER_ID = uuid.UUID("39480b3b-0d60-4c98-9b8f-2048e6356e1d")
PREFLIGHT_URL = f"{BASE_URL}/{ORDER_ID}/third-diagnostic-canary"
SUBMIT_URL = f"{PREFLIGHT_URL}/submit"


def _order():
    return SimpleNamespace(
        id=ORDER_ID,
        state="ready",
        attempt_count=2,
    )


def _status(*, eligible=False, reason="third_diagnostic_canary_disabled"):
    return ThirdDiagnosticCanaryPreflight(
        eligible=eligible,
        reason=reason,
        prior_attempt_count=2,
        circuit_open=False,
        third_gate_enabled=eligible,
        first_gate_disabled=True,
        second_gate_disabled=True,
        controlled_gate_enabled=eligible,
        target_matches=eligible,
        placeorder_enabled=eligible,
    )


def _assert_no_third_submit_form(html):
    assert f'action="{SUBMIT_URL}"' not in html
    assert f"action='{SUBMIT_URL}'" not in html
    assert "Executar UMA terceira" not in html


def test_third_preflight_requires_admin(client):
    response = client.get(PREFLIGHT_URL)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/login")


def test_third_preflight_is_get_only_and_has_no_submit_route(
    client, admin_user, login, monkeypatch
):
    login(admin_user.email)
    monkeypatch.setattr(admin_mod, "_load_order", lambda order_id: _order())
    monkeypatch.setattr(
        admin_mod,
        "get_third_diagnostic_canary_preflight",
        lambda ali_order, *, session: _status(),
    )

    get_response = client.get(PREFLIGHT_URL)
    post_same_path = client.post(PREFLIGHT_URL)
    post_submit_path = client.post(SUBMIT_URL)

    assert get_response.status_code == 200
    assert post_same_path.status_code == 405
    assert post_submit_path.status_code == 404
    assert "READ-ONLY" in get_response.text
    assert "BLOCKED / NOT ARMED" in get_response.text
    assert "third_diagnostic_canary_disabled" in get_response.text
    _assert_no_third_submit_form(get_response.text)


def test_third_preflight_can_display_ready_without_exposing_execution(
    client, admin_user, login, monkeypatch
):
    login(admin_user.email)
    monkeypatch.setattr(admin_mod, "_load_order", lambda order_id: _order())
    monkeypatch.setattr(
        admin_mod,
        "get_third_diagnostic_canary_preflight",
        lambda ali_order, *, session: _status(
            eligible=True,
            reason="ready_for_third_diagnostic_canary",
        ),
    )

    response = client.get(PREFLIGHT_URL)

    assert response.status_code == 200
    assert "READY — PREFLIGHT ONLY" in response.text
    assert "ready_for_third_diagnostic_canary" in response.text
    assert "Não é autorização" in response.text
    assert "third-diagnostic-canary/submit" in response.text
    _assert_no_third_submit_form(response.text)


def test_third_preflight_rolls_back_even_when_evaluation_raises(
    client, admin_user, login, monkeypatch
):
    login(admin_user.email)
    monkeypatch.setattr(admin_mod, "_load_order", lambda order_id: _order())

    rollback_calls = {"count": 0}

    def _rollback():
        rollback_calls["count"] += 1

    monkeypatch.setattr(admin_mod.db.session, "rollback", _rollback)

    def _raise(*args, **kwargs):
        raise RuntimeError("sanitized-preflight-failure")

    monkeypatch.setattr(admin_mod, "get_third_diagnostic_canary_preflight", _raise)

    with pytest.raises(RuntimeError, match="sanitized-preflight-failure"):
        client.get(PREFLIGHT_URL)

    assert rollback_calls["count"] == 1
