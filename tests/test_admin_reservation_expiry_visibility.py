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

# Relógio congelado: nenhum teste depende da data real de execução.
# São Paulo está em UTC-3 o ano todo (sem DST), então as conversões abaixo
# são determinísticas.
FROZEN_NOW = datetime(2030, 6, 15, 10, 0)  # naive UTC
FIXED_EXPIRY_UTC = datetime(2030, 6, 15, 15, 30)  # naive UTC
FIXED_EXPIRY_SP = "15/06/2030 às 12:30"
FIXED_CREATED_UTC = datetime(2030, 6, 10, 12, 0)  # naive UTC
FIXED_CREATED_SP = "10/06/2030 às 09:00"


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """Congela o relógio do serviço de reservas no FROZEN_NOW."""
    monkeypatch.setattr(
        "loja.services.stock_reservations.utcnow", lambda: FROZEN_NOW
    )
    return FROZEN_NOW


@pytest.fixture(autouse=True)
def _clean_dashboard_singletons(db):
    """Ações redirecionam ao dashboard, que cria singletons no 1º acesso."""
    from loja.modules.models import ContactInfo, FooterConfig, SiteConfig

    known = {
        model: {row[0] for row in db.session.query(model.id).all()}
        for model in (SiteConfig, FooterConfig, ContactInfo)
    }
    yield
    for model, ids in known.items():
        for row in model.query.all():
            if row.id not in ids:
                db.session.delete(row)
    db.session.commit()

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
        created_at=created_at or FROZEN_NOW,
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

    response = client.get("/admin/painel/checkout-recovery")

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
    _reservation_order(db, user, product, expires_at=FROZEN_NOW + timedelta(hours=5))
    login(user.email, "senha123")

    html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)
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
        db, user, product, expires_at=FROZEN_NOW - timedelta(hours=1)
    )
    login(user.email, "senha123")

    html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)

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

    html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)

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
        expires_at=FROZEN_NOW + timedelta(hours=5),
        status="paid",
    )
    released = _reservation_order(
        db,
        user,
        product,
        expires_at=FROZEN_NOW + timedelta(hours=5),
        reserved=False,
    )
    login(user.email, "senha123")

    html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)

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
        expires_at=FROZEN_NOW + timedelta(hours=5),
        with_payment="approved",
    )
    login(user.email, "senha123")

    html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)

    assert order.order_number not in _active_section(html)


def test_get_performs_no_mutations(
    client, db, admin_user, login, make_product
):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product

    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(
        db, user, product, expires_at=FROZEN_NOW + timedelta(hours=5)
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

    response = client.get("/admin/painel/checkout-recovery")

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


def _arm_boundary_sentinels(monkeypatch):
    """Sentinelas explícitos: qualquer toque em fronteira externa é registrado."""
    import smtplib

    import loja.integrations.aliexpress.fulfillment as fulfillment
    import loja.modules.getway_mercado_pago.payment_service as payment_service

    calls = []

    def _recorder(name):
        def _record(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"fronteira externa tocada: {name}")

        return _record

    monkeypatch.setattr(
        payment_service, "create_preference", _recorder("mp.create_preference")
    )
    monkeypatch.setattr(
        payment_service, "get_payment", _recorder("mp.get_payment")
    )
    monkeypatch.setattr(payment_service, "get_sdk", _recorder("mp.get_sdk"))
    monkeypatch.setattr(
        fulfillment,
        "ensure_aliexpress_fulfillment",
        _recorder("aliexpress.ensure_fulfillment"),
    )
    monkeypatch.setattr(smtplib, "SMTP", _recorder("smtp.SMTP"))
    return calls


def test_get_calls_no_external_boundaries(
    client, db, admin_user, login, make_product, monkeypatch
):
    user = admin_user
    product = make_product(stock=5)
    _reservation_order(db, user, product, expires_at=FROZEN_NOW + timedelta(hours=5))
    login(user.email, "senha123")

    calls = _arm_boundary_sentinels(monkeypatch)

    response = client.get("/admin/painel/checkout-recovery")

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
        expires_at=FROZEN_NOW + timedelta(hours=5),
        with_payment="pending",
    )
    login(user.email, "senha123")

    with caplog.at_level("WARNING"):
        html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)

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
        db, user, product, expires_at=FROZEN_NOW - timedelta(hours=1)
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
    kwargs = {"expires_at": FROZEN_NOW + timedelta(hours=5)}
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
        db, user, product, expires_at=FROZEN_NOW + timedelta(hours=5)
    )
    expired = _reservation_order(
        db, user, product, expires_at=FROZEN_NOW - timedelta(hours=1)
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


def test_expiry_equal_now_is_active_not_releasable(
    client, db, admin_user, login, make_product, frozen_now
):
    """expires_at == now: ativa (vigente) e nunca liberável."""
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.services.stock_reservations import (
        find_active_stock_reservations,
        find_expired_stock_reservations,
        release_single_expired_stock_reservation,
    )

    user = admin_user
    product = make_product(stock=5)
    order = _reservation_order(db, user, product, expires_at=frozen_now)
    login(user.email, "senha123")

    with db.session.no_autoflush:
        assert order.id in [o.id for o in find_active_stock_reservations()]
        assert order.id not in [
            o.id for o in find_expired_stock_reservations(hours=24)
        ]

    html = client.get("/admin/painel/checkout-recovery").get_data(as_text=True)
    assert order.order_number in _active_section(html)

    assert (
        release_single_expired_stock_reservation(order.id, now=frozen_now)
        is False
    )
    db.session.refresh(order)
    db.session.refresh(product)
    assert order.stock_reserved is True
    assert product.stock == 4

    response = client.post(
        f"/admin/reservas-expiradas/{order.id}/liberar",
        data={"confirm_release": "1"},
        follow_redirects=True,
    )
    assert "nenhum estoque foi alterado" in response.get_data(as_text=True).lower()
    db.session.refresh(order)
    assert order.stock_reserved is True
    assert Order.query.filter_by(id=order.id).count() == 1


def test_null_with_approved_latest_excluded_from_count(
    db, make_user, make_product, frozen_now
):
    from loja.services.stock_reservations import count_unproven_stock_reservations

    user = make_user()
    product = make_product(stock=10)
    before = count_unproven_stock_reservations()
    _reservation_order(
        db, user, product, expires_at=None, with_payment="approved"
    )

    # Pagamento latest-first aprovado decide: fora da contagem fail-closed.
    assert count_unproven_stock_reservations() == before


def test_latest_first_approved_old_rejected_new(
    db, make_user, make_product, frozen_now
):
    import loja.services.stock_reservations as reservations
    from loja.modules.getway_mercado_pago.payment_models import Payment

    user = make_user()
    product = make_product(stock=5)
    order = _reservation_order(
        db, user, product, expires_at=frozen_now + timedelta(hours=5)
    )
    db.session.add(
        Payment(
            id=uuid.uuid4(),
            order_id=order.id,
            provider_payment_id="prov-old-approved-%s" % uuid.uuid4().hex,
            status="approved",
            amount=Decimal("30.00"),
            currency="BRL",
            raw_payload={},
            created_at=frozen_now - timedelta(hours=2),
        )
    )
    db.session.add(
        Payment(
            id=uuid.uuid4(),
            order_id=order.id,
            provider_payment_id="prov-new-rejected-%s" % uuid.uuid4().hex,
            status="rejected",
            amount=Decimal("30.00"),
            currency="BRL",
            raw_payload={},
            created_at=frozen_now - timedelta(hours=1),
        )
    )
    db.session.commit()

    # Latest-first: o rejected mais recente vence; pedido segue ativo.
    assert order.id in [o.id for o in reservations.find_active_stock_reservations()]


def test_created_at_tie_resolved_by_id_desc(db, make_user, make_product, frozen_now):
    import loja.services.stock_reservations as reservations
    from loja.modules.getway_mercado_pago.payment_models import Payment

    user = make_user()
    product = make_product(stock=5)
    order = _reservation_order(
        db, user, product, expires_at=frozen_now + timedelta(hours=5)
    )
    tied_at = frozen_now - timedelta(minutes=30)
    db.session.add(
        Payment(
            id=uuid.UUID(int=1),
            order_id=order.id,
            provider_payment_id="prov-tie-approved",
            status="approved",
            amount=Decimal("30.00"),
            currency="BRL",
            raw_payload={},
            created_at=tied_at,
        )
    )
    db.session.add(
        Payment(
            id=uuid.UUID(int=2),
            order_id=order.id,
            provider_payment_id="prov-tie-rejected",
            status="rejected",
            amount=Decimal("30.00"),
            currency="BRL",
            raw_payload={},
            created_at=tied_at,
        )
    )
    db.session.commit()

    # Mesmo created_at: vence o maior id (rejected) → pedido segue ativo.
    assert order.id in [o.id for o in reservations.find_active_stock_reservations()]


def test_payment_lookups_bounded_not_linear(db, make_user, make_product, frozen_now):
    """Consultas de pagamento não crescem com o nº de pedidos (sem N+1)."""
    from sqlalchemy import event

    from loja import db as _db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_active_stock_reservations,
    )

    user = make_user()
    product = make_product(stock=1000)
    for _ in range(200):
        _reservation_order(
            db, user, product, expires_at=frozen_now + timedelta(hours=5)
        )

    def _counted(fn):
        seen = []

        def _before(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        event.listen(_db.engine, "before_cursor_execute", _before)
        try:
            result = fn()
        finally:
            event.remove(_db.engine, "before_cursor_execute", _before)
        return result, len(seen)

    _, small_find = _counted(
        lambda: find_active_stock_reservations(limit=10)
    )
    found, big_find = _counted(
        lambda: find_active_stock_reservations(limit=500)
    )
    _, big_count = _counted(count_unproven_stock_reservations)

    assert len(found) >= 200
    assert big_find == small_find  # constante, não linear
    assert big_find <= 3  # pedidos + pagamentos (+1 de folga)
    assert big_count <= 3
