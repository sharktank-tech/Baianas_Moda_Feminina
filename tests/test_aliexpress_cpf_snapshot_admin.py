"""FASE 8.0.12B — admin-only offline CPF snapshot surface."""

from types import SimpleNamespace

import pytest

from loja.views import aliexpress_cpf_snapshot_admin as view


ORDER_ID = "39480b3b-0d60-4c98-9b8f-2048e6356e1d"
ROUTE = f"/admin/integrations/aliexpress/pedidos/{ORDER_ID}/cpf-snapshot"
TEST_CPF = "52998224725"


def _order():
    return SimpleNamespace(
        id=ORDER_ID,
        state="ready",
        attempt_count=5,
        ali_order_id=None,
        submission_snapshot=SimpleNamespace(shipping_address={}),
    )


def _login_admin(login, admin_user):
    response = login(admin_user.email)
    assert response.status_code == 200


def test_regular_user_cannot_access_surface(client, login, regular_user):
    login(regular_user.email)
    response = client.get(ROUTE, follow_redirects=False)
    assert response.status_code in (302, 403)


def test_get_is_sanitized_and_does_not_expose_cpf(client, login, admin_user, monkeypatch):
    _login_admin(login, admin_user)
    order = _order()
    order.submission_snapshot.shipping_address = {"cpf": TEST_CPF}
    monkeypatch.setattr(view, "_load_order", lambda order_id: order)
    monkeypatch.setattr(
        view,
        "_sanitized_status",
        lambda ali_order: {
            "state": "ready",
            "attempt_count": 5,
            "has_cpf": True,
            "revision_count": 2,
            "has_remote_identity": False,
        },
    )

    response = client.get(ROUTE)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "PRESENTE" in body
    assert TEST_CPF not in body


def test_post_delegates_once_and_never_echoes_cpf(client, login, admin_user, monkeypatch):
    _login_admin(login, admin_user)
    order = _order()
    calls = []

    monkeypatch.setattr(view, "_load_order", lambda order_id: order)
    monkeypatch.setattr(
        view,
        "revise_submission_snapshot_cpf",
        lambda ali_order, *, cpf, session=None: calls.append((ali_order, cpf, session)),
    )
    monkeypatch.setattr(
        view,
        "_sanitized_status",
        lambda ali_order: {
            "state": "ready",
            "attempt_count": 5,
            "has_cpf": True,
            "revision_count": 2,
            "has_remote_identity": False,
        },
    )

    response = client.post(ROUTE, data={"cpf": TEST_CPF}, follow_redirects=True)
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] is order
    assert calls[0][1] == TEST_CPF
    assert TEST_CPF not in response.get_data(as_text=True)


def test_post_sanitizes_validation_error(client, login, admin_user, monkeypatch):
    _login_admin(login, admin_user)
    order = _order()
    monkeypatch.setattr(view, "_load_order", lambda order_id: order)

    from loja.integrations.aliexpress.submission_snapshot import SubmissionSnapshotError

    def blocked(*args, **kwargs):
        raise SubmissionSnapshotError("shipping_cpf_invalid")

    monkeypatch.setattr(view, "revise_submission_snapshot_cpf", blocked)
    monkeypatch.setattr(
        view,
        "_sanitized_status",
        lambda ali_order: {
            "state": "ready",
            "attempt_count": 5,
            "has_cpf": False,
            "revision_count": 1,
            "has_remote_identity": False,
        },
    )

    response = client.post(ROUTE, data={"cpf": TEST_CPF}, follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "shipping_cpf_invalid" in body
    assert TEST_CPF not in body
