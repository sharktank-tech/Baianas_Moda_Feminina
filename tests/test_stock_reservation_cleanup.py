import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from loja.utils import utcnow


def _reserved_order(db, user, product, *, age_hours=25, status="pending_payment"):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    product.stock = int(product.stock or 0) - 1
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status=status,
        subtotal=Decimal("20.00"),
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=Decimal("30.00"),
        stock_reserved=True,
        customer_name="Cliente Teste",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        created_at=utcnow() - timedelta(hours=age_hours),
        mp_preference_expires_at=utcnow() - timedelta(
            hours=max(age_hours - 24, -1)
        ),
    )
    order.items.append(
        OrderItem(
            id=uuid.uuid4(),
            product_id=product.id,
            product_name=product.name,
            selected_size="M",
            quantity=1,
            unit_price=Decimal("20.00"),
            line_total=Decimal("20.00"),
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.fixture(autouse=True)
def _cleanup_session(db):
    yield
    db.session.rollback()
    db.session.remove()


def test_release_expired_pending_is_idempotent(db, make_user, make_product):
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    user = make_user()
    product = make_product(stock=5)
    order = _reserved_order(db, user, product)
    assert product.stock == 4

    first = release_single_expired_stock_reservation(
        order.id,
        source="test",
        now=utcnow(),
    )
    db.session.refresh(order)
    db.session.refresh(product)
    assert first is True
    assert order.status == "pending_payment"
    assert order.stock_reserved is False
    assert product.stock == 5

    second = release_single_expired_stock_reservation(
        order.id,
        source="test",
        now=utcnow(),
    )
    db.session.refresh(product)
    assert second is False
    assert product.stock == 5

@pytest.mark.parametrize(
    ("age_hours", "status", "reserved"),
    [(2, "pending_payment", True), (25, "paid", True), (25, "pending_payment", False)],
)
def test_release_skips_ineligible_orders(
    db, make_user, make_product, age_hours, status, reserved
):
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    user = make_user()
    product = make_product(stock=5)
    order = _reserved_order(
        db,
        user,
        product,
        age_hours=age_hours,
        status=status,
    )
    if not reserved:
        order.stock_reserved = False
        product.stock = 5
        db.session.commit()

    before = product.stock
    released = release_single_expired_stock_reservation(
        order.id,
        source="test",
    )
    db.session.refresh(order)
    db.session.refresh(product)
    assert released is False
    assert order.stock_reserved is reserved
    assert product.stock == before


def test_configuration_bounds(app, monkeypatch):
    from loja.services.stock_reservations import (
        load_release_limit,
        load_reservation_hours,
    )

    with app.app_context():
        assert load_reservation_hours("24") == 24
        assert load_release_limit("100") == 100
        for raw in ("0", "721", "abc", ""):
            if raw == "":
                assert load_reservation_hours(raw) == 24
            else:
                with pytest.raises(ValueError, match="stock_reservation_hours_invalid"):
                    load_reservation_hours(raw)
        for raw in ("0", "501", "abc"):
            with pytest.raises(
                ValueError,
                match="stock_reservation_release_limit_invalid",
            ):
                load_release_limit(raw)


def test_cron_requires_secret_and_gate(
    client, app, db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=5)
    order = _reserved_order(db, user, product)

    monkeypatch.setenv("CRON_SECRET", "cron-test-secret")
    app.config["STOCK_RESERVATION_RELEASE_ENABLED"] = False

    assert client.get(
        "/internal/maintenance/release-expired-reservations"
    ).status_code == 401
    assert client.get(
        "/internal/maintenance/release-expired-reservations",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401

    disabled = client.get(
        "/internal/maintenance/release-expired-reservations",
        headers={"Authorization": "Bearer cron-test-secret"},
    )
    assert disabled.status_code == 200
    assert disabled.get_json() == {
        "ok": True,
        "enabled": False,
        "released": 0,
    }
    db.session.refresh(order)
    assert order.stock_reserved is True

    app.config["STOCK_RESERVATION_RELEASE_ENABLED"] = True
    app.config["STOCK_RESERVATION_HOURS"] = "24"
    enabled = client.get(
        "/internal/maintenance/release-expired-reservations",
        headers={"Authorization": "Bearer cron-test-secret"},
    )
    assert enabled.status_code == 200
    assert enabled.get_json()["released"] == 1
    db.session.refresh(order)
    assert order.stock_reserved is False


def test_admin_action_releases_without_provider_call(
    client, app, db, admin_user, login, make_product, monkeypatch
):
    import loja.modules.getway_mercado_pago.payment_service as payment_service

    app.config["STOCK_RESERVATION_HOURS"] = "24"
    app.config["STOCK_RESERVATION_RELEASE_LIMIT"] = "100"
    product = make_product(stock=5)
    order = _reserved_order(db, admin_user, product)
    provider_calls = []
    monkeypatch.setattr(
        payment_service,
        "get_payment",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    login(admin_user.email)
    response = client.post(
        f"/admin/reservas-expiradas/{order.id}/liberar",
        data={"confirm_release": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.session.refresh(order)
    db.session.refresh(product)
    assert order.status == "pending_payment"
    assert order.stock_reserved is False
    assert product.stock == 5
    assert provider_calls == []


def test_admin_action_denies_non_admin(
    client, db, make_user, login, make_product
):
    user = make_user()
    product = make_product(stock=5)
    order = _reserved_order(db, user, product)
    login(user.email)

    response = client.post(
        f"/admin/reservas-expiradas/{order.id}/liberar",
        data={"confirm_release": "1"},
        follow_redirects=False,
    )
    assert response.status_code in {302, 403}
    db.session.refresh(order)
    assert order.stock_reserved is True


def test_admin_dashboard_links_checkout_recovery_on_desktop_and_mobile(
    client, admin_user, login
):
    login(admin_user.email)

    response = client.get("/admin")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('href="/admin/recuperacao-checkout"') == 2
    assert "Recuperação de checkout" in html
    assert ">\n        Recuperação\n" in html
