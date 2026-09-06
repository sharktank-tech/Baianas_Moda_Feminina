"""Admin safety queue for the controlled AliExpress live canary (8.0.11H).

GET-only tests. No provider, no network and no mutation endpoint is introduced.
"""

import uuid

import pytest

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt
from loja.utils import utcnow


BASE_URL = "/admin/integrations/aliexpress/pedidos"
SAFETY_URL = f"{BASE_URL}/canary-safety"


def _clear_blocking_rows():
    db.session.query(AliExpressSubmissionAttempt).delete()
    db.session.query(AliExpressOrder).delete()
    db.session.commit()


@pytest.fixture(autouse=True)
def _isolated_blocking_tables(db):
    _clear_blocking_rows()
    yield
    _clear_blocking_rows()


def _blocking_attempt(*, state="unknown"):
    now = utcnow()
    order = AliExpressOrder(
        id=uuid.uuid4(),
        state="pending_reconciliation",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        attempt_key=f"attempt-key-MUST-NOT-LEAK-{uuid.uuid4().hex[:8]}",
        attempt_count=1,
        queued_at=now,
        last_attempt_at=now,
        last_error="submission_unknown",
        created_at=now,
        updated_at=now,
    )
    db.session.add(order)
    db.session.flush()
    attempt = AliExpressSubmissionAttempt(
        id=uuid.uuid4(),
        aliexpress_order_id=order.id,
        attempt_key=order.attempt_key,
        state=state,
        started_at=now,
        completed_at=now if state == "unknown" else None,
        last_error="submission_unknown" if state == "unknown" else None,
        created_at=now,
        updated_at=now,
    )
    db.session.add(attempt)
    db.session.commit()
    return order, attempt


def test_canary_safety_requires_admin(client):
    resp = client.get(SAFETY_URL)
    assert resp.headers["Location"].endswith("/login")


def test_canary_safety_unknown_opens_circuit_and_hides_attempt_key(
    client, admin_user, login
):
    order, _ = _blocking_attempt(state="unknown")
    login(admin_user.email)

    resp = client.get(SAFETY_URL)

    assert resp.status_code == 200
    assert "Circuito ABERTO" in resp.text
    assert "unknown" in resp.text
    assert (str(order.id)[:8]) in resp.text
    assert "attempt-key-MUST-NOT-LEAK" not in resp.text
    assert "remote_order_id" not in resp.text
    assert "Retry só é permitido" in resp.text


def test_canary_safety_in_flight_without_age_release(client, admin_user, login):
    _blocking_attempt(state="in_flight")
    login(admin_user.email)

    resp = client.get(SAFETY_URL)

    assert resp.status_code == 200
    assert "Circuito ABERTO" in resp.text
    assert "in_flight" in resp.text
    assert "não liberar por idade" in resp.text


def test_fulfillment_index_surfaces_open_circuit(client, admin_user, login):
    _blocking_attempt(state="unknown")
    login(admin_user.email)

    resp = client.get(BASE_URL)

    assert resp.status_code == 200
    assert "Canary safety: BLOQUEADO" in resp.text
    assert "Circuit breaker" in resp.text
    assert "ABERTO" in resp.text
    assert "attempt-key-MUST-NOT-LEAK" not in resp.text


def test_canary_safety_closed_without_blockers(client, admin_user, login):
    login(admin_user.email)

    resp = client.get(SAFETY_URL)

    assert resp.status_code == 200
    assert "Circuito FECHADO" in resp.text
    assert "Nenhuma tentativa bloqueadora" in resp.text
    assert "não habilita AliExpress placeorder" in resp.text
