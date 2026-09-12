"""FASE 8.0.12C — CPF do checkout antes do Mercado Pago."""

import uuid
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import (
    Order,
    OrderItem,
    _normalize_order_cpf_for_checkout,
    _order_requires_aliexpress_phone,
)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    db.session.remove()


def _order(user, *, cpf=None):
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
        customer_phone="63998765432",
        customer_cpf=cpf,
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


def _mark_aliexpress(db, product):
    product.source = "aliexpress"
    product.is_draft = False
    product.pricing_status = "priced"
    db.session.commit()
    return product


def test_aliexpress_checkout_without_cpf_is_blocked(db, make_user, make_product):
    user = make_user()
    product = _mark_aliexpress(db, make_product())
    order = _order(user)
    _attach_item(order, product)

    assert _order_requires_aliexpress_phone(db.session, order) is True
    with pytest.raises(ValueError, match="CPF válido"):
        _normalize_order_cpf_for_checkout(
            order,
            requires_aliexpress=True,
            raw_cpf="",
        )


def test_aliexpress_checkout_invalid_cpf_does_not_echo_value(db, make_user, make_product):
    user = make_user()
    product = _mark_aliexpress(db, make_product())
    order = _order(user)
    _attach_item(order, product)
    raw = "123.456.789-00"

    with pytest.raises(ValueError) as exc:
        _normalize_order_cpf_for_checkout(
            order,
            requires_aliexpress=True,
            raw_cpf=raw,
        )
    assert "CPF válido" in str(exc.value)
    assert raw not in str(exc.value)


def test_aliexpress_checkout_valid_cpf_is_normalized_and_persisted(db, make_user, make_product):
    user = make_user()
    product = _mark_aliexpress(db, make_product())
    order = _order(user)
    _attach_item(order, product)

    normalized = _normalize_order_cpf_for_checkout(
        order,
        requires_aliexpress=True,
        raw_cpf="529.982.247-25",
    )

    assert normalized == "52998224725"
    assert order.customer_cpf == "52998224725"


def test_non_aliexpress_checkout_keeps_cpf_optional(db, make_user, make_product):
    user = make_user()
    product = make_product()
    order = _order(user)
    _attach_item(order, product)

    assert _order_requires_aliexpress_phone(db.session, order) is False
    assert _normalize_order_cpf_for_checkout(
        order,
        requires_aliexpress=False,
        raw_cpf="",
    ) is None
    assert order.customer_cpf is None
