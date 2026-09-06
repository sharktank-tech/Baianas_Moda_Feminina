"""Admin safety surface for Fase 8.0.11I.

The tests do not arm gates and do not perform provider/network I/O.
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


def _order(*, state="ready", attempt_key=None, remote_id=None):
    now = utcnow()
    row = AliExpressOrder(
        id=uuid.uuid4(),
        state=state,
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        attempt_key=attempt_key,
        attempt_count=0,
        ali_order_id=remote_id,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_single_live_canary_preflight_requires_admin(client):
    row = _order()
    response = client.get(f"{BASE_URL}/{row.id}/single-live-canary")
    assert response.headers["Location"].endswith("/login")


def test_single_live_canary_page_is_fail_closed_and_sanitized(
    client, admin_user, login
):
    secret_attempt = "attempt-key-MUST-NOT-LEAK"
    remote_identity = "998877665544332211"
    row = _order(attempt_key=secret_attempt, remote_id=remote_identity)
    login(admin_user.email)

    response = client.get(f"{BASE_URL}/{row.id}/single-live-canary")

    assert response.status_code == 200
    assert "BLOCKED / NOT ARMED" in response.text
    assert "Single live canary" in response.text
    assert secret_attempt not in response.text
    assert remote_identity not in response.text
    assert "Executar UMA tentativa real" in response.text
    assert "disabled" in response.text


def test_single_live_canary_post_is_csrf_protected(client, admin_user, login):
    row = _order()
    login(admin_user.email)

    response = client.post(f"{BASE_URL}/{row.id}/single-live-canary/submit")

    assert response.status_code == 400
