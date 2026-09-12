"""FASE 8.0.12C — congela CPF persistido no shipping snapshot novo."""

import uuid
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import AliExpressOrder
from loja.integrations.aliexpress.submission_snapshot import (
    AliExpressSubmissionSnapshot,
    SubmissionSnapshotError,
)
from loja.integrations.aliexpress.snapshot_checkout_tax_id import _freeze_checkout_cpf


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    db.session.remove()


def _source_order(db, make_user, *, cpf):
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="paid",
        subtotal=Decimal("49.90"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("49.90"),
        stock_reserved=False,
        customer_name="Cliente Teste",
        customer_email=user.email,
        customer_phone="63998765432",
        customer_cpf=cpf,
        zip_code="77000000",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="Palmas",
        state="TO",
    )
    db.session.add(order)
    db.session.flush()

    ali_order = AliExpressOrder(
        id=uuid.uuid4(),
        order_id=order.id,
        state="ready",
        attempt_count=0,
    )
    db.session.add(ali_order)
    db.session.flush()
    return order, ali_order


def _snapshot(ali_order):
    return AliExpressSubmissionSnapshot(
        id=uuid.uuid4(),
        aliexpress_order_id=ali_order.id,
        shipping_address={
            "contact_person": "Cliente Teste",
            "full_name": "Cliente Teste",
            "mobile_no": "63998765432",
            "phone_country": "+55",
            "zip": "77000000",
            "address": "Rua Teste 10 Centro",
            "city": "Palmas",
            "province": "TO",
            "country": "BR",
        },
        items=[{
            "order_item_id": str(uuid.uuid4()),
            "product_id": "100000001",
            "sku_attr": "14:771",
            "product_count": 1,
        }],
    )


def test_freeze_checkout_cpf_adds_only_canonical_value(db, make_user):
    _, ali_order = _source_order(db, make_user, cpf="52998224725")
    snapshot = _snapshot(ali_order)
    old_items = list(snapshot.items)

    _freeze_checkout_cpf(db.session, snapshot)

    assert snapshot.shipping_address["cpf"] == "52998224725"
    assert snapshot.items == old_items
    assert "52998224725" not in repr(snapshot)


def test_freeze_checkout_cpf_blocks_missing_value_without_echo(db, make_user):
    _, ali_order = _source_order(db, make_user, cpf=None)
    snapshot = _snapshot(ali_order)

    with pytest.raises(SubmissionSnapshotError) as exc:
        _freeze_checkout_cpf(db.session, snapshot)

    assert exc.value.args == ("shipping_cpf_invalid",)


def test_freeze_checkout_cpf_blocks_invalid_value_without_echo(db, make_user):
    raw = "12345678900"
    _, ali_order = _source_order(db, make_user, cpf=raw)
    snapshot = _snapshot(ali_order)

    with pytest.raises(SubmissionSnapshotError) as exc:
        _freeze_checkout_cpf(db.session, snapshot)

    assert exc.value.args == ("shipping_cpf_invalid",)
    assert raw not in str(exc.value)
