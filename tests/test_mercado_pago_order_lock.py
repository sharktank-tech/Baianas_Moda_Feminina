"""Scoping PostgreSQL row lock of ``_sync_payment`` to the orders table.

``Order`` loads ``User`` and ``Coupon`` through ``lazy="joined"``
relationships. Because ``coupon_id`` is nullable, PostgreSQL rejects a plain
``SELECT ... FOR UPDATE`` over the resulting outer join ("FOR UPDATE cannot be
applied to the nullable side of an outer join"). The lock must be restricted
to ``pollomodas.orders`` alone via ``with_for_update(of=Order)``.
"""

import uuid
from decimal import Decimal

import pytest

import loja.views.payments_routes as pr
from loja.modules.getway_mercado_pago.payment_models import Order


@pytest.fixture(autouse=True)
def _cleanup_db(db):
    yield
    db.session.rollback()
    db.session.remove()


def _item_data(product, quantity=2):
    unit_price = Decimal(str(product.price))
    return [
        {
            "product_id": product.id,
            "product_name": product.name,
            "selected_size": "M",
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": unit_price * quantity,
        }
    ]


def _make_order(db, user, product):
    from loja.modules.getway_mercado_pago.payment_models import OrderItem

    unit_price = Decimal(str(product.price))
    quantity = 2
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=unit_price * quantity,
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=unit_price * quantity + Decimal("10.00"),
        stock_reserved=True,
        customer_name="Cliente Teste",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    order.items.append(
        OrderItem(
            id=uuid.uuid4(),
            product_id=product.id,
            product_name=product.name,
            selected_size="M",
            quantity=quantity,
            unit_price=unit_price,
            line_total=unit_price * quantity,
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


def _mp_payload(payment_id, order, status, detail):
    return {
        "id": payment_id,
        "external_reference": str(order.id),
        "transaction_amount": str(order.total),
        "currency_id": "BRL",
        "status": status,
        "status_detail": detail,
    }


def test_sync_payment_order_lock_scoped_to_orders_table(
    db,
    make_user,
    make_product,
    monkeypatch,
):
    from sqlalchemy.orm.query import Query

    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product)

    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 8

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    lock_calls = []
    original = Query.with_for_update

    def _spy(self, *args, **kwargs):
        lock_calls.append((self.column_descriptions, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", _spy, raising=False)

    synced = pr._sync_payment("lock-9001")
    assert synced.status == "paid"
    assert product.stock == 8

    order_locks = [
        (desc[0]["entity"], kwargs)
        for desc, kwargs in lock_calls
        if desc and desc[0]["entity"] is Order
    ]
    assert len(order_locks) == 1, lock_calls
    _, kwargs = order_locks[0]
    assert kwargs.get("of") is Order