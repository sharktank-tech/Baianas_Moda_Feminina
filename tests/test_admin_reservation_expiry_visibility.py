"""Visibilidade da expiração de reservas no painel admin.

Somente leitura: seção "Reservas de estoque ativas" com expiração
comprovada futura, sem botão de liberação; vencidas restritas à tabela
de vencidas; legadas sem expiração apenas em contagem fail-closed.
Nenhuma migração, pagamento, preferência nova ou liberação de estoque.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from loja.utils import utcnow

# Datas fixas com folga no futuro (UTC-3 o ano todo em São Paulo, sem DST).
FIXED_EXPIRY_UTC = datetime(2030, 6, 15, 15, 30)  # naive UTC
FIXED_EXPIRY_SP = "15/06/2030 às 12:30"
FIXED_CREATED_UTC = datetime(2030, 6, 10, 12, 0)  # naive UTC
FIXED_CREATED_SP = "10/06/2030 às 09:00"

SECRET_CPF = "52998224725"
SECRET_PHONE = "+5563987654321"
SECRET_STREET = "Rua Secreta 999"
SECRET_COMPLEMENT = "Apto 666"
SECRET_PREFERENCE_ID = "pref-SECRET-123"
SECRET_CHECKOUT_URL = "https://mp.secret/checkout/xyz"
SECRET_MP_PAYMENT_ID = "pay-SECRET-456"
SECRET_PROVIDER_PAYMENT_ID = "prov-SECRET-789"


def _reservation_order(
    db,
    user,
    product,
    *,
    expires_at,
    status="pending_payment",
    reserved=True,
    created_at=None,
    with_payment=None,
):
    """Pedido com reserva e dados sensíveis preenchidos."""
    from loja.modules.getway_mercado_pago.payment_models import (
        Order,
        OrderItem,
        Payment,
    )

    if reserved:
        product.stock = int(product.stock or 0) - 1
    tag = uuid.uuid4().hex[:12]
    secrets = {
        "cpf": SECRET_CPF,
        "phone": SECRET_PHONE,
        "street": SECRET_STREET,
        "complement": SECRET_COMPLEMENT,
        "preference_id": f"pref-SECRET-{tag}",
        "checkout_url": f"https://mp.secret/checkout/{tag}",
        "mp_payment_id": f"pay-SECRET-{tag}",
        "provider_payment_id": f"prov-SECRET-{tag}",
    }
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status=status,
        subtotal=Decimal("20.00"),
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=Decimal("30.00"),
        stock_reserved=reserved,
        customer_name="Cliente Teste",
        customer_email=user.email,
        customer_cpf=secrets["cpf"],
        customer_phone=secrets["phone"],
        zip_code="01310100",
        street=secrets["street"],
        number="10",
        complement=secrets["complement"],
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        created_at=created_at or utcnow(),
        mp_preference_id=secrets["preference_id"],
        mp_checkout_url=secrets["checkout_url"],
        mp_payment_id=secrets["mp_payment_id"],
        mp_preference_expires_at=expires_at,
    )
    order._secret_values = secrets
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
    db.session.flush()
    if with_payment is not None:
        db.session.add(
            Payment(
                id=uuid.uuid4(),
                order_id=order.id,
                provider_payment_id=secrets["provider_payment_id"],
                status=with_payment,
                amount=Decimal("30.00"),
                currency="BRL",
                raw_payload={},
            )
        )
    db.session.commit()
    return order


@pytest.fixture(autouse=True)
def _cleanup_session(db):
    yield
    db.session.rollback()
    db.session.remove()


def _active_section(html):
    """Recorta somente a seção de reservas ativas."""
    start = html.index("Reservas de estoque ativas")
    end = html.index("Reservas de estoque vencidas")
    assert start < end
    return html[start:end]


def _expired_section(html):
    start = html.index("Reservas de estoque vencidas")
    return html[start:]


def test_active_future_shows_correct_sp_time(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db,
        user,
        product,
        expires_at=FIXED_EXPIRY_UTC,
        created_at=FIXED_CREATED_UTC,
    )
    login(user.email, "senha123")

    response = client.get("/admin/recuperacao-checkout")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    section = _active_section(html)
    assert order.order_number in section
    assert FIXED_EXPIRY_SP in section
    assert FIXED_CREATED_SP in section
    assert "ATIVA" in section


def test_active_section_has_no_release_controls(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=5)
    _reservation_order(db, user, product, expires_at=utcnow() + timedelta(hours=5))
    login(user.email, "senha123")

    html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)
    section = _active_section(html)

    assert "<form" not in section
    assert "Liberar reserva" not in section
    assert "liberar" not in section.lower()
    assert "confirm_release" not in section


def test_expired_only_in_expired_table(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db, user, product, expires_at=utcnow() - timedelta(hours=1)
    )
    login(user.email, "senha123")

    html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)

    assert order.order_number not in _active_section(html)
    assert order.order_number in _expired_section(html)


def test_null_expiry_only_in_failclosed_count(
    client, db, admin_user, login, make_product
):
    from loja.services.stock_reservations import count_unproven_stock_reservations

    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(db, user, product, expires_at=None)
    login(user.email, "senha123")

    html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)

    assert order.order_number not in html
    assert "Reservas sem expiração comprovada" in html
    # Contagem fail-closed: delta relativo (BD compartilhado na sessão).
    before = count_unproven_stock_reservations()
    _reservation_order(db, user, product, expires_at=None)
    assert count_unproven_stock_reservations() == before + 1


def test_paid_and_released_absent_everywhere(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=10)
    paid = _reservation_order(
        db,
        user,
        product,
        expires_at=utcnow() + timedelta(hours=5),
        status="paid",
    )
    released = _reservation_order(
        db,
        user,
        product,
        expires_at=utcnow() + timedelta(hours=5),
        reserved=False,
    )
    login(user.email, "senha123")

    html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)

    assert paid.order_number not in html
    assert released.order_number not in html


def test_approved_payment_absent_from_active(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db,
        user,
        product,
        expires_at=utcnow() + timedelta(hours=5),
        with_payment="approved",
    )
    login(user.email, "senha123")

    html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)

    assert order.order_number not in _active_section(html)


def test_get_performs_no_mutations(
    client, db, admin_user, login, make_product
):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product

    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db, user, product, expires_at=utcnow() + timedelta(hours=5)
    )
    null_order = _reservation_order(db, user, product, expires_at=None)
    login(user.email, "senha123")

    before = (
        Order.query.count(),
        OrderItem.query.count(),
        product.stock,
        order.stock_reserved,
        null_order.stock_reserved,
        Product.query.count(),
    )

    response = client.get("/admin/recuperacao-checkout")

    assert response.status_code == 200
    db.session.expire_all()
    assert (
        Order.query.count(),
        OrderItem.query.count(),
        db.session.get(Product, product.id).stock,
        db.session.get(Order, order.id).stock_reserved,
        db.session.get(Order, null_order.id).stock_reserved,
        Product.query.count(),
    ) == before


def test_get_calls_no_external_boundaries(
    client, db, admin_user, login, make_product, monkeypatch
):
    import smtplib

    import loja.modules.getway_mercado_pago.payment_service as payment_service

    user = admin_user
    product = make_product(stock=5)
    _reservation_order(db, user, product, expires_at=utcnow() + timedelta(hours=5))
    login(user.email, "senha123")

    calls = []
    monkeypatch.setattr(
        payment_service,
        "create_preference",
        lambda *args, **kwargs: calls.append("create_preference"),
    )
    monkeypatch.setattr(
        payment_service,
        "get_payment",
        lambda *args, **kwargs: calls.append("get_payment"),
    )
    monkeypatch.setattr(
        smtplib,
        "SMTP",
        lambda *args, **kwargs: calls.append("smtp"),
    )

    response = client.get("/admin/recuperacao-checkout")

    assert response.status_code == 200
    assert calls == []


def test_no_sensitive_data_in_html_or_logs(
    client, db, admin_user, login, make_product, caplog
):
    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db,
        user,
        product,
        expires_at=utcnow() + timedelta(hours=5),
        with_payment="pending",
    )
    login(user.email, "senha123")

    with caplog.at_level("WARNING"):
        html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)

    assert order.order_number in html  # número público pode aparecer
    for secret in order._secret_values.values():
        assert secret not in html
        assert secret not in caplog.text


def test_batch_release_gate_stays_off(
    client, app, db, admin_user, login, make_product
):
    from loja.modules.getway_mercado_pago.payment_models import Order

    app.config["STOCK_RESERVATION_BATCH_RELEASE_ENABLED"] = False
    app.config["STOCK_RESERVATION_RELEASE_ENABLED"] = False
    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db, user, product, expires_at=utcnow() - timedelta(hours=1)
    )
    before = product.stock
    login(user.email, "senha123")

    response = client.post(
        "/admin/reservas-expiradas/liberar", follow_redirects=True
    )

    assert "desligada" in response.get_data(as_text=True).lower()
    db.session.refresh(order)
    db.session.refresh(product)
    assert order.stock_reserved is True
    assert product.stock == before


@pytest.mark.parametrize(
    "case",
    ["null_expiry", "future_expiry", "paid", "released", "approved_payment"],
)
def test_forged_single_release_refused(
    client, db, admin_user, login, make_product, case
):
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = admin_user
    product = make_product(stock=10)
    kwargs = {"expires_at": utcnow() + timedelta(hours=5)}
    if case == "null_expiry":
        kwargs = {"expires_at": None}
    elif case == "paid":
        kwargs.update(status="paid")
    elif case == "released":
        kwargs.update(reserved=False)
    elif case == "approved_payment":
        kwargs.update(with_payment="approved")
    order = _reservation_order(db, user, product, **kwargs)
    reserved_before = order.stock_reserved
    stock_before = product.stock
    login(user.email, "senha123")

    response = client.post(
        f"/admin/reservas-expiradas/{order.id}/liberar",
        data={"confirm_release": "1"},
        follow_redirects=True,
    )

    assert "nenhum estoque foi alterado" in response.get_data(as_text=True).lower()
    db.session.refresh(order)
    db.session.refresh(product)
    assert order.stock_reserved is reserved_before
    assert product.stock == stock_before
    assert Order.query.filter_by(id=order.id).count() == 1


def test_service_finder_parity(db, make_user, make_product):
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_active_stock_reservations,
        find_expired_stock_reservations,
    )

    user = make_user()
    product = make_product(stock=10)
    active = _reservation_order(
        db, user, product, expires_at=utcnow() + timedelta(hours=5)
    )
    expired = _reservation_order(
        db, user, product, expires_at=utcnow() - timedelta(hours=1)
    )
    legacy = _reservation_order(db, user, product, expires_at=None)

    # Escopo por usuário (BD compartilhado na sessão de testes).
    mine_active = {
        o.id for o in find_active_stock_reservations() if o.user_id == user.id
    }
    mine_expired = {
        o.id
        for o in find_expired_stock_reservations(hours=24)
        if o.user_id == user.id
    }
    assert mine_active == {active.id}
    assert mine_expired == {expired.id}
    assert legacy.id not in mine_active
    assert legacy.id not in mine_expired
    # Legado casa o predicado fail-closed (mesmo filtro, escopo do usuário).
    from loja.modules.getway_mercado_pago.payment_models import Order

    mine_unproven = {
        o.id
        for o in Order.query.filter(
            Order.user_id == user.id,
            Order.stock_reserved.is_(True),
            Order.status == "pending_payment",
            Order.mp_preference_expires_at.is_(None),
        ).all()
    }
    assert legacy.id in mine_unproven
    assert count_unproven_stock_reservations() >= 1
