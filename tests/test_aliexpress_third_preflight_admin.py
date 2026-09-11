"""FASE 8.0.11S — admin POST surface for the third AliExpress canary.

No test enables provider HTTP. The POST exists, is admin/CSRF protected and is
blocked by an independent final execution hard-off by default.
"""

from types import SimpleNamespace
import uuid

import pytest

from loja.integrations.aliexpress import third_diagnostic_execution as execution_mod
from loja.integrations.aliexpress.third_diagnostic_canary import (
    ThirdDiagnosticCanaryPreflight,
)
from loja.integrations.aliexpress.third_diagnostic_execution import (
    ThirdDiagnosticExecutionStatus,
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


def _execution_status(*, enabled=False):
    return ThirdDiagnosticExecutionStatus(
        enabled=enabled,
        reason=(
            "third_diagnostic_execution_authorized"
            if enabled
            else "third_diagnostic_execution_hard_off"
        ),
    )


def test_third_preflight_requires_admin(client):
    response = client.get(PREFLIGHT_URL)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/login")


def test_third_submit_requires_admin(client):
    response = client.post(SUBMIT_URL)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/login")


def test_third_preflight_displays_independent_hard_off(
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
    monkeypatch.setattr(
        admin_mod,
        "get_third_diagnostic_execution_status",
        lambda: _execution_status(enabled=False),
    )

    response = client.get(PREFLIGHT_URL)

    assert response.status_code == 200
    assert "READY — EXISTING PREFLIGHT ONLY" in response.text
    assert "Final execution layer: HARD-OFF" in response.text
    assert "third_diagnostic_execution_hard_off" in response.text
    assert f'action="{SUBMIT_URL}"' in response.text
    assert "disabled" in response.text
    assert "Nenhum gate é armado pela aplicação" in response.text


def test_third_submit_is_post_only(client, admin_user, login):
    login(admin_user.email)
    response = client.get(SUBMIT_URL)
    assert response.status_code == 405


def test_third_submit_hard_off_never_reaches_third_boundary(
    client, admin_user, login, monkeypatch
):
    login(admin_user.email)
    monkeypatch.setattr(admin_mod, "_load_order", lambda order_id: _order())
    monkeypatch.delenv(
        "ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED", raising=False
    )

    calls = {"count": 0}

    def _must_not_delegate(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("third diagnostic boundary must remain unreachable")

    monkeypatch.setattr(
        execution_mod,
        "submit_third_diagnostic_canary_order",
        _must_not_delegate,
    )

    response = client.post(SUBMIT_URL)

    assert response.status_code == 303
    assert response.headers["Location"].endswith(
        f"/{ORDER_ID}/third-diagnostic-canary"
    )
    assert calls["count"] == 0


def test_third_submit_is_covered_by_application_csrf(
    client, app, admin_user, login, monkeypatch
):
    login(admin_user.email)
    monkeypatch.setattr(admin_mod, "_load_order", lambda order_id: _order())
    monkeypatch.setattr(
        admin_mod,
        "execute_third_diagnostic_canary_order",
        lambda *args, **kwargs: pytest.fail("CSRF must reject before view execution"),
    )

    previous = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(SUBMIT_URL)
    finally:
        app.config["WTF_CSRF_ENABLED"] = previous

    assert response.status_code == 400


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


def test_third_preflight_hides_sensitive_execution_data(
    client, admin_user, login, monkeypatch
):
    login(admin_user.email)
    monkeypatch.setattr(admin_mod, "_load_order", lambda order_id: _order())
    monkeypatch.setattr(
        admin_mod,
        "get_third_diagnostic_canary_preflight",
        lambda ali_order, *, session: _status(),
    )
    monkeypatch.setattr(
        admin_mod,
        "get_third_diagnostic_execution_status",
        lambda: _execution_status(),
    )

    response = client.get(PREFLIGHT_URL)

    assert response.status_code == 200
    lowered = response.text.lower()
    assert "attempt-key-must-not-leak" not in lowered
    assert "remote_order_id" not in lowered
    assert "access_token" not in lowered
    assert "app_secret" not in lowered
