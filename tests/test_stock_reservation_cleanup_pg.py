"""Concorrência real: pagamento x liberação de reserva em PostgreSQL.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_stock_cleanup_* e nunca toca public ou produção.
"""

import secrets
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    _url = make_url(DATABASE_URL) if DATABASE_URL else None
    IS_SAFE_POSTGRES = bool(
        _url
        and _url.get_backend_name() == "postgresql"
        and _url.host in {"127.0.0.1", "localhost", "::1"}
        and str(_url.database or "").startswith("pollo")
    )
except Exception:
    IS_SAFE_POSTGRES = False

pytestmark = pytest.mark.skipif(
    not IS_SAFE_POSTGRES,
    reason="requer PostgreSQL descartável loopback pollo*",
)


@pytest.fixture
def pg_app(monkeypatch):
    schema = "pollo_test_stock_cleanup_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "stock-cleanup-pg")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {"options": "-csearch_path=%s" % schema},
        },
    )
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", schema)

    from loja import create_app, db

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()

    yield app

    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _materialize(app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product, User
    from loja.utils import utcnow

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Stock Cleanup",
            email="stock-%s@pg.test" % uuid.uuid4().hex,
            is_admin=False,
        )
        user.set_password("senha123")
        product = Product(
            id=uuid.uuid4(),
            name="Produto PG reserva",
            description="sintético",
            price=Decimal("20.00"),
            stock=4,
            source="manual",
            is_draft=True,
            is_active=False,
        )
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="pending_payment",
            subtotal=Decimal("20.00"),
            shipping=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("30.00"),
            stock_reserved=True,
            customer_name="Cliente sintético",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua sintética",
            number="10",
            neighborhood="Centro",
            city="São Paulo",
            state="SP",
            created_at=utcnow() - timedelta(hours=25),
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
        db.session.add_all([user, product, order])
        db.session.commit()
        return order.id, product.id


def test_payment_lock_wins_cleanup_skips(pg_app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    order_id, product_id = _materialize(pg_app)
    payment_locked = threading.Event()
    allow_payment_commit = threading.Event()
    errors = []

    def payment_worker():
        try:
            with pg_app.app_context():
                order = (
                    Order.query.filter_by(id=order_id)
                    .with_for_update()
                    .one()
                )
                payment_locked.set()
                assert allow_payment_commit.wait(10)
                order.status = "paid"
                db.session.commit()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=payment_worker)
    thread.start()
    assert payment_locked.wait(10)

    with pg_app.app_context():
        result = release_expired_stock_reservations(
            hours=24,
            source="pg_test",
        )
        assert result.released == 0

    allow_payment_commit.set()
    thread.join(10)
    assert not thread.is_alive()
    assert errors == []

    with pg_app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        assert order.status == "paid"
        assert order.stock_reserved is True
        assert product.stock == 4


def test_cleanup_lock_wins_late_payment_reserves_once(pg_app, monkeypatch):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    import loja.services.stock_reservations as reservations

    order_id, product_id = _materialize(pg_app)
    cleanup_locked = threading.Event()
    allow_cleanup = threading.Event()
    errors = []
    original_release = reservations._release_stock_locked

    def paused_release(order):
        cleanup_locked.set()
        assert allow_cleanup.wait(10)
        return original_release(order)

    monkeypatch.setattr(reservations, "_release_stock_locked", paused_release)

    def cleanup_worker():
        try:
            with pg_app.app_context():
                reservations.release_expired_stock_reservations(
                    hours=24,
                    source="pg_test",
                )
        except Exception as exc:
            errors.append(exc)

    def payment_worker():
        try:
            with pg_app.app_context():
                order = (
                    Order.query.filter_by(id=order_id)
                    .with_for_update()
                    .one()
                )
                if not order.stock_reserved:
                    product = (
                        Product.query.filter_by(id=product_id)
                        .with_for_update()
                        .one()
                    )
                    assert product.stock >= 1
                    product.stock -= 1
                    order.stock_reserved = True
                order.status = "paid"
                db.session.commit()
        except Exception as exc:
            errors.append(exc)

    cleanup = threading.Thread(target=cleanup_worker)
    cleanup.start()
    assert cleanup_locked.wait(10)

    payment = threading.Thread(target=payment_worker)
    payment.start()
    allow_cleanup.set()

    cleanup.join(10)
    payment.join(10)
    assert not cleanup.is_alive()
    assert not payment.is_alive()
    assert errors == []

    with pg_app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        assert order.status == "paid"
        assert order.stock_reserved is True
        assert product.stock == 4
