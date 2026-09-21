import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

import loja.views.payments_routes as pr
from loja.utils import utcnow


@pytest.fixture(autouse=True)
def _cleanup_db(db):
    """Encerra transações entre testes para evitar lock do SQLite em arquivo."""
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


def _make_order(db, user, product, status="pending_payment", stock_reserved=True, quantity=2):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    unit_price = Decimal(str(product.price))
    created_at = utcnow()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status=status,
        subtotal=unit_price * quantity,
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=unit_price * quantity + Decimal("10.00"),
        stock_reserved=stock_reserved,
        created_at=created_at,
        mp_preference_expires_at=created_at + timedelta(hours=24),
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


def test_reserve_stock_decrements_and_validates(db, make_product):
    product = make_product(stock=5)
    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 3


def test_reserve_stock_raises_when_insufficient(db, make_product):
    product = make_product(stock=1)
    try:
        pr._reserve_stock(_item_data(product, quantity=2))
    except ValueError as error:
        assert "insuficiente" in str(error)
    else:
        raise AssertionError("Deveria falhar por estoque insuficiente")
    assert product.stock == 1


def test_release_stock_restores_and_clears_flag(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product, quantity=2)

    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 8

    pr._release_stock(order)
    assert product.stock == 10
    assert order.stock_reserved is False


def test_approved_payment_does_not_double_decrement(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product, quantity=2)

    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 8

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    synced = pr._sync_payment("9001")
    assert synced.status == "paid"
    assert product.stock == 8


def test_refunded_payment_releases_reservation(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product, quantity=2)

    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 8

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "refunded", "refunded"),
    )

    synced = pr._sync_payment("9002")
    assert synced.status == "refunded"
    assert product.stock == 10
    assert synced.stock_reserved is False


def test_rejected_payment_releases_reservation(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product, quantity=2)

    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 8

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "rejected", "rejected"),
    )

    synced = pr._sync_payment("9003")
    assert synced.status == "payment_failed"
    assert product.stock == 10
    assert synced.stock_reserved is False


def test_release_expired_reservations(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    stale = _make_order(db, user, product, quantity=2)
    stale.created_at = utcnow() - timedelta(hours=48)
    stale.mp_preference_expires_at = utcnow() - timedelta(hours=24)
    db.session.commit()

    fresh_product = make_product(stock=20, name="Camiseta Nova")
    fresh = _make_order(db, user, fresh_product, quantity=1)
    pr._reserve_stock(_item_data(fresh_product, quantity=1))

    pr._reserve_stock(_item_data(product, quantity=2))
    assert product.stock == 8

    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    release_expired_stock_reservations(hours=24)

    assert product.stock == 10
    assert stale.stock_reserved is False
    assert fresh.stock_reserved is True
    assert fresh_product.stock == 19


def test_checkout_failure_rolls_back_reservation(db, client, make_user, make_product, monkeypatch):
    user = make_user()
    product = make_product(stock=5)

    # Cotação simulada: o foco aqui é o rollback da reserva, não a tarifa.
    monkeypatch.setattr(pr, "quote_checkout_shipping", lambda item_data, cep: Decimal("10.00"))
    from test_shipping_quote import preset_shipping_quote

    preset_shipping_quote(
        client, "01310100",
        [{"product_id": product.id, "quantity": 2}],
        "10.00",
    )

    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product",
            "product_id": str(product.id),
            "quantity": 2,
            "selected_size": "M",
            "nome": "Cliente Teste",
            "email": user.email,
            "cep": "01310100",
            "estado": "SP",
            "endereco": "Rua Teste",
            "numero": "10",
            "bairro": "Centro",
            "cidade": "São Paulo",
        },
        follow_redirects=True,
    )
    assert "não foi possível abrir o pagamento" in resp.get_data(as_text=True).lower()
    assert product.stock == 5