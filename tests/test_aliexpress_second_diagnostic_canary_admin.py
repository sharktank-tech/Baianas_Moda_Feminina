"""Admin safety surface for FASE 8.0.11K.

No test arms real-provider gates or performs network I/O.
"""

import uuid

import pytest

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt
from loja.utils import utcnow


BASE_URL = "/admin/integrations/aliexpress/pedidos"


@pytest.fixture(autouse=True)
def _isolate_orders(db):
    db.session.query(AliExpressSubmissionAttempt).delete()
    db.session.query(AliExpressOrder).delete()
    db.session.commit()
    yield
    db.session.query(AliExpressSubmissionAttempt).delete()
    db.session.query(AliExpressOrder).delete()
    db.session.commit()


def _order(*, state="ready", attempt_key=None, remote_id=None, attempt_count=1):
    now = utcnow()
    row = AliExpressOrder(
        id=uuid.uuid4(),
        state=state,
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        attempt_key=attempt_key,
        attempt_count=attempt_count,
        ali_order_id=remote_id,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_second_diagnostic_preflight_requires_admin(client):
    row = _order()
    response = client.get(f"{BASE_URL}/{row.id}/second-diagnostic-canary")
    assert response.headers["Location"].endswith("/login")


def test_second_diagnostic_page_is_fail_closed_and_sanitized(
    client, admin_user, login
):
    secret_attempt = "attempt-key-MUST-NOT-LEAK"
    remote_identity = "998877665544332211"
    row = _order(attempt_key=secret_attempt, remote_id=remote_identity)
    login(admin_user.email)

    response = client.get(f"{BASE_URL}/{row.id}/second-diagnostic-canary")

    assert response.status_code == 200
    assert "BLOCKED / NOT ARMED" in response.text
    assert "Second diagnostic canary" in response.text
    assert secret_attempt not in response.text
    assert remote_identity not in response.text
    assert "Executar UMA segunda tentativa diagnóstica real" in response.text
    assert "disabled" in response.text


def test_second_diagnostic_post_is_csrf_protected(
    app, client, admin_user, login, monkeypatch
):
    row = _order()
    login(admin_user.email)
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)

    response = client.post(
        f"{BASE_URL}/{row.id}/second-diagnostic-canary/submit"
    )

    assert response.status_code == 400
