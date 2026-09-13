"""FASE 8.1.0 — elegibilidade read-only antes do orquestrador de produção."""

import uuid
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    AliExpressRemoteOrder,
    AliExpressSubmissionAttempt,
)
from loja.integrations.aliexpress.production_preflight import (
    PREFLIGHT_ATTEMPT_UNRESOLVED,
    PREFLIGHT_LEDGER_MISMATCH,
    PREFLIGHT_REMOTE_IDENTITY,
    PREFLIGHT_SHIPPING_INVALID,
    PREFLIGHT_SOURCE_NOT_PAID,
    PREFLIGHT_STATE_NOT_READY,
    evaluate_production_submission_eligibility,
)
from loja.integrations.aliexpress.submission_snapshot import AliExpressSubmissionSnapshot
from loja.utils import utcnow


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    db.session.remove()


def _target(db, make_user, *, source_status="paid", state="ready", cpf="52998224725"):
    user = make_user()
    source = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status=source_status,
        subtotal=Decimal("49.90"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("49.90"),
        stock_reserved=False,
        customer_name="Cliente Teste",
        customer_email=user.email,
        customer_phone="11999999999",
        customer_cpf=cpf,
        zip_code="01001000",
        street="Rua Teste",
        number="10",
        complement="Apto 1",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    db.session.add(source)
    db.session.flush()

    ali_order = AliExpressOrder(
        id=uuid.uuid4(),
        order_id=source.id,
        supplier_id=uuid.uuid4(),
        state=state,
        attempt_count=0,
    )
    db.session.add(ali_order)
    db.session.flush()

    item = AliExpressOrderItem(
        id=uuid.uuid4(),
        aliexpress_order_id=ali_order.id,
        order_item_id=uuid.uuid4(),
        supplier_product_external_id="100000001",
        supplier_sku_external_id="SKU001",
        supplier_sku_attr="14:771",
        quantity=1,
        state="ready",
    )
    db.session.add(item)
    db.session.flush()

    snapshot = AliExpressSubmissionSnapshot(
        id=uuid.uuid4(),
        aliexpress_order_id=ali_order.id,
        shipping_address={
            "contact_person": "Cliente Teste",
            "full_name": "Cliente Teste",
            "mobile_no": "11999999999",
            "phone_country": "+55",
            "zip": "01001000",
            "address": "Rua Teste 10 Centro",
            "address2": "Apto 1",
            "city": "São Paulo",
            "province": "SP",
            "country": "BR",
            "cpf": cpf,
        },
        items=[{
            "order_item_id": str(item.order_item_id),
            "product_id": "100000001",
            "sku_attr": "14:771",
            "product_count": 1,
        }],
    )
    db.session.add(snapshot)
    db.session.commit()
    return source, ali_order, snapshot


def _evaluate(monkeypatch, db, ali_order):
    # O preflight reutiliza o gate canônico de integridade/credencial. Estes
    # testes isolam o contrato novo; cobertura do gate existente vive na suíte 8.0.
    monkeypatch.setattr(
        "loja.integrations.aliexpress.production_preflight._submission_gate",
        lambda order, items, refresher=None: None,
    )
    return evaluate_production_submission_eligibility(ali_order, session=db.session)


def test_paid_ready_valid_snapshot_is_eligible_and_read_only(db, make_user, monkeypatch):
    _, ali_order, _ = _target(db, make_user)
    before = (
        ali_order.state,
        ali_order.attempt_count,
        ali_order.attempt_key,
        ali_order.last_error,
    )

    result = _evaluate(monkeypatch, db, ali_order)

    assert result.eligible is True
    assert result.code is None
    assert result.attempt_count == 0
    assert result.attempt_rows == 0
    assert result.snapshot_present is True
    db.session.refresh(ali_order)
    assert (
        ali_order.state,
        ali_order.attempt_count,
        ali_order.attempt_key,
        ali_order.last_error,
    ) == before
    assert AliExpressSubmissionAttempt.query.filter_by(
        aliexpress_order_id=ali_order.id
    ).count() == 0


def test_source_order_must_still_be_paid(db, make_user, monkeypatch):
    _, ali_order, _ = _target(db, make_user, source_status="refunded")
    result = _evaluate(monkeypatch, db, ali_order)
    assert result.eligible is False
    assert result.code == PREFLIGHT_SOURCE_NOT_PAID


def test_aliexpress_order_must_be_exactly_ready(db, make_user, monkeypatch):
    _, ali_order, _ = _target(db, make_user, state="blocked")
    result = _evaluate(monkeypatch, db, ali_order)
    assert result.eligible is False
    assert result.code == PREFLIGHT_STATE_NOT_READY


def test_unresolved_attempt_blocks_without_new_attempt(db, make_user, monkeypatch):
    _, ali_order, _ = _target(db, make_user)
    ali_order.attempt_count = 1
    db.session.add(AliExpressSubmissionAttempt(
        id=uuid.uuid4(),
        aliexpress_order_id=ali_order.id,
        attempt_key=uuid.uuid4().hex * 2,
        state="unknown",
        started_at=utcnow(),
        completed_at=None,
    ))
    db.session.commit()

    result = _evaluate(monkeypatch, db, ali_order)

    assert result.eligible is False
    assert result.code == PREFLIGHT_ATTEMPT_UNRESOLVED
    assert AliExpressSubmissionAttempt.query.filter_by(
        aliexpress_order_id=ali_order.id
    ).count() == 1


def test_remote_identity_blocks(db, make_user, monkeypatch):
    _, ali_order, _ = _target(db, make_user)
    db.session.add(AliExpressRemoteOrder(
        id=uuid.uuid4(),
        aliexpress_order_id=ali_order.id,
        remote_order_id="123456789",
        position=0,
    ))
    db.session.commit()

    result = _evaluate(monkeypatch, db, ali_order)
    assert result.eligible is False
    assert result.code == PREFLIGHT_REMOTE_IDENTITY


def test_attempt_ledger_must_match_attempt_count(db, make_user, monkeypatch):
    _, ali_order, _ = _target(db, make_user)
    ali_order.attempt_count = 1
    db.session.commit()

    result = _evaluate(monkeypatch, db, ali_order)
    assert result.eligible is False
    assert result.code == PREFLIGHT_LEDGER_MISMATCH
    assert result.attempt_count == 1
    assert result.attempt_rows == 0


def test_snapshot_without_cpf_is_not_production_eligible(db, make_user, monkeypatch):
    _, ali_order, snapshot = _target(db, make_user)
    shipping = dict(snapshot.shipping_address)
    shipping.pop("cpf")
    snapshot.shipping_address = shipping
    db.session.commit()

    result = _evaluate(monkeypatch, db, ali_order)
    assert result.eligible is False
    assert result.code == PREFLIGHT_SHIPPING_INVALID


def test_invalid_cpf_never_echoes_value(db, make_user, monkeypatch):
    _, ali_order, snapshot = _target(db, make_user)
    raw = "12345678900"
    shipping = dict(snapshot.shipping_address)
    shipping["cpf"] = raw
    snapshot.shipping_address = shipping
    db.session.commit()

    result = _evaluate(monkeypatch, db, ali_order)
    assert result.eligible is False
    assert result.code == PREFLIGHT_SHIPPING_INVALID
    assert raw not in repr(result)
