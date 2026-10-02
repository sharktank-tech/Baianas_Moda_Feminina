import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

import loja.views.payments_routes as pr
from loja.utils import utcnow


@pytest.fixture(autouse=True)
def _cleanup_db(db):
    yield
    db.session.rollback()
    db.session.remove()


def _make_order(
    db,
    user,
    product,
    *,
    status="pending_payment",
    stock_reserved=True,
    quantity=2,
    expires_at=None,
):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    unit_price = Decimal(str(product.price))
    created_at = utcnow() - timedelta(hours=30)
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
        mp_preference_expires_at=(
            expires_at
            if expires_at is not None
            else utcnow() - timedelta(hours=26)
        ),
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


# ── Estado na liberação ──────────────────────────────────────────────────────

def test_release_expiration_transitions_order_to_expired(db, make_user, make_product):
    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    user = make_user()
    product = make_product(stock=10)
    product.stock = 8
    order = _make_order(db, user, product, quantity=2)

    result = release_expired_stock_reservations(source="test")

    assert result.released == 1
    assert result.skipped == 0
    assert order.status == "expired"
    assert order.stock_reserved is False
    assert product.stock == 10

    again = release_expired_stock_reservations(source="test")
    assert again.candidates == 0
    assert order.status == "expired"


def test_release_keeps_pending_when_payment_approved(db, make_user, make_product):
    from loja.modules.getway_mercado_pago.payment_models import Payment
    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    user = make_user()
    product = make_product(stock=10)
    product.stock = 8
    order = _make_order(db, user, product, quantity=2)
    db.session.add(
        Payment(
            id=uuid.uuid4(),
            order_id=order.id,
            provider_payment_id="expired-state-approved",
            status="approved",
            amount=order.total,
            currency="BRL",
        )
    )
    db.session.commit()

    result = release_expired_stock_reservations(source="test")

    assert result.released == 0
    assert result.skipped == 1
    assert order.status == "pending_payment"
    assert order.stock_reserved is True
    assert product.stock == 8


def test_single_release_also_transitions_to_expired(db, make_user, make_product):
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    user = make_user()
    product = make_product(stock=10)
    product.stock = 8
    order = _make_order(db, user, product, quantity=2)

    released = release_single_expired_stock_reservation(order.id, source="test")

    assert released is True
    assert order.status == "expired"
    assert order.stock_reserved is False
    assert product.stock == 10


# ── Webhook tardio sobre expired ─────────────────────────────────────────────

def test_late_approved_payment_flips_expired_to_paid(db, make_user, make_product, monkeypatch):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(
        db,
        user,
        product,
        status="expired",
        stock_reserved=False,
    )

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    synced = pr._sync_payment("expired-approved-1")

    assert synced.status == "paid"
    assert synced.stock_reserved is True
    assert product.stock == 8


def test_pending_notification_keeps_expired(db, make_user, make_product, monkeypatch):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(
        db,
        user,
        product,
        status="expired",
        stock_reserved=False,
    )

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "pending", "pending_contingency"),
    )

    synced = pr._sync_payment("expired-pending-1")

    assert synced.status == "expired"
    assert synced.stock_reserved is False
    assert product.stock == 10


def test_rejected_notification_keeps_expired(db, make_user, make_product, monkeypatch):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(
        db,
        user,
        product,
        status="expired",
        stock_reserved=False,
    )

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "rejected", "cc_rejected"),
    )

    synced = pr._sync_payment("expired-rejected-1")

    assert synced.status == "expired"
    assert synced.stock_reserved is False
    assert product.stock == 10


# ── Projeção para o cliente ──────────────────────────────────────────────────

def test_expired_bucket_labels_and_filters(db, make_user, make_product):
    from loja.views.routes import (
        PURCHASE_FILTERS,
        PURCHASE_STATUS_LABELS,
        _order_purchase_bucket,
        _purchase_status_counts,
    )

    user = make_user()
    product = make_product()
    order = _make_order(db, user, product, status="expired", stock_reserved=False)

    assert _order_purchase_bucket(order) == "expirado"
    assert PURCHASE_STATUS_LABELS["expirado"] == "Expirado"
    assert PURCHASE_FILTERS["expirados"]["bucket"] == "expirado"
    assert PURCHASE_FILTERS["expirados"]["titulo"] == "Pedidos expirados"
    assert _purchase_status_counts([order])["expirado"] == 1


def test_expired_order_never_reenters_reservation_views(db, make_user, make_product):
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_active_stock_reservations,
        find_expired_stock_reservations,
    )

    user = make_user()
    product = make_product()
    before = count_unproven_stock_reservations()

    # Estado defensivo: mesmo com reserva e prazo NULL, "expired" fica
    # fora das três visões (todas exigem pending_payment).
    order = _make_order(db, user, product, status="expired", stock_reserved=True)
    order.mp_preference_expires_at = None
    db.session.commit()

    assert count_unproven_stock_reservations() == before
    assert order.id not in [
        o.id for o in find_expired_stock_reservations()
    ]
    assert order.id not in [
        o.id for o in find_active_stock_reservations()
    ]


# ── Área do cliente (rotas/templates) ────────────────────────────────────────

def test_expired_texts_render_in_list_and_detail(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product()
    order = _make_order(db, user, product, status="expired", stock_reserved=False)

    login(user.email)

    response = client.get("/encomendas")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Expirado" in html
    assert "Refazer pedido" in html

    response = client.get(f"/encomendas/pedido/{order.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Pedido expirado" in html
    assert (
        "O prazo de pagamento acabou e o estoque foi liberado."
        in html
    )
    assert "Refazer pedido" in html


def test_expirados_filter_page_lists_expired_order(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product()
    _make_order(db, user, product, status="expired", stock_reserved=False)
    _make_order(
        db,
        user,
        product,
        status="pending_payment",
        stock_reserved=True,
        expires_at=utcnow() + timedelta(hours=12),
    )

    login(user.email)

    response = client.get("/encomendas/expirados")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Pedidos expirados" in html
    assert "Refazer pedido" in html


def test_account_dashboard_shows_expired_card(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product()
    _make_order(db, user, product, status="expired", stock_reserved=False)

    login(user.email)

    response = client.get("/conta")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Pedido expirado" in html


# ── Refazer pedido ───────────────────────────────────────────────────────────

def test_refazer_pedido_adds_items_and_opens_checkout(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product, status="expired", stock_reserved=False)

    login(user.email)

    response = client.post(
        f"/encomendas/pedido/{order.id}/refazer",
        follow_redirects=True,
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert product.name in html

    from loja.modules.models import Cart

    cart = Cart.query.filter_by(user_id=user.id).first()
    assert cart is not None
    assert sum(item.quantity for item in cart.items) == 2


def test_refazer_pedido_blocked_for_non_expired(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product()
    order = _make_order(
        db,
        user,
        product,
        status="pending_payment",
        stock_reserved=True,
        expires_at=utcnow() + timedelta(hours=12),
    )

    login(user.email)

    response = client.post(f"/encomendas/pedido/{order.id}/refazer")

    assert response.status_code == 302
    assert f"/encomendas/pedido/{order.id}" in response.headers["Location"]
    assert order.status == "pending_payment"

    from loja.modules.models import Cart

    assert Cart.query.filter_by(user_id=user.id).first() is None


def test_refazer_pedido_forbidden_for_other_user(client, db, make_user, make_product, login):
    owner = make_user()
    product = make_product()
    order = _make_order(db, owner, product, status="expired", stock_reserved=False)
    other = make_user()

    login(other.email)

    response = client.post(f"/encomendas/pedido/{order.id}/refazer")
    assert response.status_code == 404


def test_refazer_pedido_without_available_items(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product, status="expired", stock_reserved=False)
    product.is_active = False
    db.session.commit()

    login(user.email)

    response = client.post(
        f"/encomendas/pedido/{order.id}/refazer",
        follow_redirects=True,
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Nenhum item deste pedido está disponível" in html

    from loja.modules.models import Cart

    assert Cart.query.filter_by(user_id=user.id).first() is None
