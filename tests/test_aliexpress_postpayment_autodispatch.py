"""FASE 8.1.0 — testes do hook pós-pagamento fail-closed."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from loja import db as app_db
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import AliExpressOrder
from loja.integrations.aliexpress.postpayment_autodispatch import (
    POSTPAYMENT_AUTODISPATCH_ENABLED_ENV,
    POSTPAYMENT_DISABLED,
    POSTPAYMENT_NOT_PAID,
    dispatch_paid_order_after_payment,
    postpayment_autodispatch_enabled,
)


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch):
    monkeypatch.delenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, raising=False)


def _order(db, make_user, *, status="paid"):
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status=status,
        subtotal=Decimal("49.90"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("49.90"),
        stock_reserved=False,
        customer_name="Cliente Teste",
        customer_email=user.email,
        customer_phone="11999999999",
        customer_cpf="52998224725",
        zip_code="01001000",
        street="Rua Teste",
        number="10",
        complement=None,
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    db.session.add(order)
    db.session.commit()
    return order


def _ali_order(db, order, *, state="ready"):
    ali = AliExpressOrder(
        id=uuid.uuid4(),
        order_id=order.id,
        supplier_id=None,
        state=state,
        attempt_count=0,
    )
    db.session.add(ali)
    db.session.commit()
    return ali


def test_gate_is_exact_and_fail_closed(monkeypatch):
    for value in (None, "", "true", "yes", "01", "TRUE"):
        if value is None:
            monkeypatch.delenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, raising=False)
        else:
            monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, value)
        assert postpayment_autodispatch_enabled() is False

    monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, "1")
    assert postpayment_autodispatch_enabled() is True


def test_disabled_gate_has_zero_side_effects(db, make_user):
    order = _order(db, make_user)
    _ali_order(db, order)
    calls = []

    result = dispatch_paid_order_after_payment(
        order,
        session=db.session,
        materialize_fn=lambda *a, **k: calls.append("snapshot"),
        orchestrate_fn=lambda *a, **k: calls.append("dispatch"),
    )

    assert result.enabled is False
    assert result.code == POSTPAYMENT_DISABLED
    assert calls == []


def test_non_paid_order_never_materializes_or_dispatches(db, make_user, monkeypatch):
    monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, "1")
    order = _order(db, make_user, status="pending_payment")
    _ali_order(db, order)
    calls = []

    result = dispatch_paid_order_after_payment(
        order,
        session=db.session,
        materialize_fn=lambda *a, **k: calls.append("snapshot"),
        orchestrate_fn=lambda *a, **k: calls.append("dispatch"),
    )

    assert result.code == POSTPAYMENT_NOT_PAID
    assert calls == []


def test_ready_order_commits_snapshot_before_single_dispatch(db, make_user, monkeypatch):
    monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, "1")
    order = _order(db, make_user)
    ali = _ali_order(db, order)
    events = []

    class SnapshotMarker:
        snapshot = object()

    def materialize(target, *, session):
        assert target.id == ali.id
        events.append("materialize")
        return SnapshotMarker()

    original_commit = db.session.commit

    def commit():
        events.append("commit")
        return original_commit()

    def orchestrate(target, *, session):
        assert target.id == ali.id
        assert "commit" in events
        events.append("dispatch")
        return SimpleNamespace(dispatched=True, state="submitted")

    monkeypatch.setattr(db.session, "commit", commit)

    result = dispatch_paid_order_after_payment(
        order,
        session=db.session,
        materialize_fn=materialize,
        orchestrate_fn=orchestrate,
    )

    assert events == ["materialize", "commit", "dispatch"]
    assert result.snapshot_count == 1
    assert result.dispatch_count == 1


def test_advanced_states_are_not_redispatched(db, make_user, monkeypatch):
    monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, "1")
    order = _order(db, make_user)
    for state in ("submitted", "submitting", "pending_reconciliation"):
        _ali_order(db, order, state=state)
    calls = []

    result = dispatch_paid_order_after_payment(
        order,
        session=db.session,
        materialize_fn=lambda *a, **k: calls.append("snapshot"),
        orchestrate_fn=lambda *a, **k: calls.append("dispatch"),
    )

    assert calls == []
    assert result.dispatch_count == 0


def test_two_ready_supplier_orders_each_dispatch_once(db, make_user, monkeypatch):
    monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, "1")
    order = _order(db, make_user)
    first = _ali_order(db, order)
    second = _ali_order(db, order)
    dispatched = []

    def materialize(target, *, session):
        return SimpleNamespace(snapshot=object())

    def orchestrate(target, *, session):
        dispatched.append(target.id)
        return SimpleNamespace(dispatched=True, state="submitted")

    result = dispatch_paid_order_after_payment(
        order,
        session=db.session,
        materialize_fn=materialize,
        orchestrate_fn=orchestrate,
    )

    assert sorted(dispatched) == sorted([first.id, second.id])
    assert len(dispatched) == 2
    assert result.dispatch_count == 2
    assert result.snapshot_count == 2


def test_orchestrator_exception_is_absorbed(db, make_user, monkeypatch):
    monkeypatch.setenv(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV, "1")
    order = _order(db, make_user)
    _ali_order(db, order)

    def boom(*args, **kwargs):
        raise RuntimeError("provider payload must never escape")

    result = dispatch_paid_order_after_payment(
        order,
        session=db.session,
        materialize_fn=lambda *a, **k: SimpleNamespace(snapshot=object()),
        orchestrate_fn=boom,
    )

    assert result.enabled is True
    assert result.dispatch_count == 0
