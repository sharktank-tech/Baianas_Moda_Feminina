"""Guard de celular do checkout AliExpress — FASE 8.0.11X.

Não chama Mercado Pago nem AliExpress. Testa somente a decisão local que deve
ocorrer antes do flush que precede ``create_preference``.
"""

import uuid
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import (
    Order,
    OrderItem,
    _normalize_order_phone_for_checkout,
    _order_requires_aliexpress_phone,
)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    db.session.remove()


def _order(user, *, phone=None):
    return Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=Decimal("49.90"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("49.90"),
        stock_reserved=False,
        customer_name="Cliente Teste",
        customer_email=user.email,
        customer_phone=phone,
        zip_code="77000000",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="Palmas",
        state="TO",
    )


def _attach_item(order, product):
    item = OrderItem(
        id=uuid.uuid4(),
        product_id=product.id,
        product_name=product.name,
        selected_size="M",
        quantity=1,
        unit_price=Decimal(str(product.price)),
        line_total=Decimal(str(product.price)),
    )
    item.product = product
    order.items.append(item)
    return item


def _mark_product_aliexpress(db, product):
    product.source = "aliexpress"
    product.is_draft = False
    product.pricing_status = "priced"
    db.session.commit()
    return product


def test_aliexpress_checkout_without_phone_is_blocked(db, make_user, make_product):
    user = make_user()
    product = _mark_product_aliexpress(db, make_product())

    order = _order(user, phone=None)
    _attach_item(order, product)

    assert _order_requires_aliexpress_phone(db.session, order) is True
    with pytest.raises(ValueError, match="celular brasileiro válido"):
        _normalize_order_phone_for_checkout(order, requires_aliexpress=True)


def test_aliexpress_checkout_with_malformed_phone_is_blocked(db, make_user, make_product):
    user = make_user()
    product = _mark_product_aliexpress(db, make_product())

    order = _order(user, phone="55 12 3456-789")
    _attach_item(order, product)

    assert _order_requires_aliexpress_phone(db.session, order) is True
    with pytest.raises(ValueError, match="celular brasileiro válido"):
        _normalize_order_phone_for_checkout(order, requires_aliexpress=True)


def test_aliexpress_checkout_plus55_is_persisted_as_national(db, make_user, make_product):
    user = make_user()
    product = _mark_product_aliexpress(db, make_product())

    order = _order(user, phone="+55 (63) 99876-5432")
    _attach_item(order, product)

    assert _order_requires_aliexpress_phone(db.session, order) is True
    normalized = _normalize_order_phone_for_checkout(order, requires_aliexpress=True)

    assert normalized == "63998765432"
    assert order.customer_phone == "63998765432"


def test_non_aliexpress_checkout_keeps_phone_optional(db, make_user, make_product):
    user = make_user()
    product = make_product()
    assert product.source == "internal"

    order = _order(user, phone=None)
    _attach_item(order, product)

    assert _order_requires_aliexpress_phone(db.session, order) is False
    assert _normalize_order_phone_for_checkout(
        order,
        requires_aliexpress=False,
    ) is None
    assert order.customer_phone is None
