"""FASE 8.1.0 — testes do circuit breaker read-only de produção."""

import uuid
from datetime import timedelta

from loja import db
from loja.integrations.aliexpress.production_circuit_breaker import (
    CIRCUIT_OPEN_CONFIG_INVALID,
    CIRCUIT_OPEN_RECONCILIATION,
    CIRCUIT_OPEN_REJECTIONS,
    CIRCUIT_OPEN_UNRESOLVED_ATTEMPT,
    REJECTION_THRESHOLD_ENV,
    WINDOW_MINUTES_ENV,
    evaluate_production_circuit_breaker,
)
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt
from loja.utils import utcnow


def _order(state="ready"):
    obj = AliExpressOrder(
        id=uuid.uuid4(),
        state=state,
        idempotency_key=uuid.uuid4().hex,
        attempt_count=0,
    )
    db.session.add(obj)
    db.session.flush()
    return obj


def _attempt(order, *, state, minutes_ago=0, completed=True):
    now = utcnow() - timedelta(minutes=minutes_ago)
    attempt = AliExpressSubmissionAttempt(
        id=uuid.uuid4(),
        aliexpress_order_id=order.id,
        attempt_key=uuid.uuid4().hex,
        state=state,
        started_at=now,
        completed_at=now if completed else None,
    )
    db.session.add(attempt)
    db.session.flush()
    return attempt


def test_closed_with_empty_ledger(app, monkeypatch):
    monkeypatch.delenv(REJECTION_THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(WINDOW_MINUTES_ENV, raising=False)
    with app.app_context():
        state = evaluate_production_circuit_breaker()
        assert state.open is False
        assert state.code is None
        assert state.rejection_threshold == 3
        assert state.window_minutes == 15


def test_pending_reconciliation_opens_circuit(app):
    with app.app_context():
        _order("pending_reconciliation")
        db.session.commit()
        state = evaluate_production_circuit_breaker()
        assert state.open is True
        assert state.code == CIRCUIT_OPEN_RECONCILIATION


def test_unresolved_attempt_opens_circuit(app):
    with app.app_context():
        order = _order()
        _attempt(order, state="in_flight", completed=False)
        db.session.commit()
        state = evaluate_production_circuit_breaker()
        assert state.open is True
        assert state.code == CIRCUIT_OPEN_UNRESOLVED_ATTEMPT


def test_three_recent_rejections_open_circuit(app):
    with app.app_context():
        order = _order()
        _attempt(order, state="rejected", minutes_ago=1)
        _attempt(order, state="rejected", minutes_ago=2)
        _attempt(order, state="rejected", minutes_ago=3)
        db.session.commit()
        state = evaluate_production_circuit_breaker()
        assert state.open is True
        assert state.code == CIRCUIT_OPEN_REJECTIONS
        assert state.recent_rejections == 3


def test_non_rejected_recent_result_resets_streak(app):
    with app.app_context():
        order = _order()
        _attempt(order, state="rejected", minutes_ago=1)
        _attempt(order, state="confirmed", minutes_ago=2)
        _attempt(order, state="rejected", minutes_ago=3)
        db.session.commit()
        state = evaluate_production_circuit_breaker()
        assert state.open is False


def test_old_rejections_do_not_open_circuit(app):
    with app.app_context():
        order = _order()
        _attempt(order, state="rejected", minutes_ago=16)
        _attempt(order, state="rejected", minutes_ago=17)
        _attempt(order, state="rejected", minutes_ago=18)
        db.session.commit()
        state = evaluate_production_circuit_breaker()
        assert state.open is False


def test_invalid_configuration_fails_closed(app, monkeypatch):
    monkeypatch.setenv(REJECTION_THRESHOLD_ENV, "0")
    with app.app_context():
        state = evaluate_production_circuit_breaker()
        assert state.open is True
        assert state.code == CIRCUIT_OPEN_CONFIG_INVALID
