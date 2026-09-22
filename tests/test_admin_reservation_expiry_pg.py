"""Visibilidade de reservas em PostgreSQL: corretude e concorrência.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
próprio e nunca toca public ou produção. Sem migração, pagamento real ou
chamada a provedor.
"""

import secrets
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL

# Mesmo relógio congelado dos testes SQLite: nada depende da data real.
FROZEN_NOW = datetime(2030, 6, 15, 10, 0)  # naive UTC


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(
        "loja.services.stock_reservations.utcnow", lambda: FROZEN_NOW
    )
    return FROZEN_NOW

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
    schema = "pollo_test_admin_expiry_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "admin-expiry-pg")
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
    from loja.modules.getway_mercado_pago.payment_models import (
        Order,
        OrderItem,
        Payment,
    )
    from loja.modules.models import Product, User

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Expiry",
            email="expiry-%s@pg.test" % uuid.uuid4().hex,
            is_admin=False,
        )
        user.set_password("senha123")
        product = Product(
            id=uuid.uuid4(),
            name="Produto PG expiração",
            description="sintético",
            price=Decimal("20.00"),
            stock=9,
            source="manual",
            is_draft=True,
            is_active=False,
        )
        db.session.add_all([user, product])
        db.session.flush()

        def _order(expires_at, status="pending_payment", reserved=True):
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
                customer_name="Cliente sintético",
                customer_email=user.email,
                zip_code="01310100",
                street="Rua sintética",
                number="10",
                neighborhood="Centro",
                city="São Paulo",
                state="SP",
                created_at=FROZEN_NOW - timedelta(hours=1),
                mp_preference_expires_at=expires_at,
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
            db.session.flush()
            return order

        active = _order(FROZEN_NOW + timedelta(hours=5))
        _order(FROZEN_NOW - timedelta(hours=1))
        _order(None)
        _order(FROZEN_NOW + timedelta(hours=5), status="paid")
        approved = _order(FROZEN_NOW + timedelta(hours=5))
        db.session.add(
            Payment(
                id=uuid.uuid4(),
                order_id=approved.id,
                provider_payment_id="prov-pg-%s" % uuid.uuid4().hex,
                status="approved",
                amount=Decimal("30.00"),
                currency="BRL",
                raw_payload={},
            )
        )
        db.session.commit()
        return active.id, product.id


def test_active_finder_correctness_pg(pg_app):
    from loja import db
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_active_stock_reservations,
        find_expired_stock_reservations,
    )

    active_id, _ = _materialize(pg_app)

    with pg_app.app_context():
        assert [o.id for o in find_active_stock_reservations()] == [active_id]
        assert all(
            o.id != active_id
            for o in find_expired_stock_reservations(hours=24)
        )
        assert count_unproven_stock_reservations() == 1


def test_concurrent_reads_no_mutation_pg(pg_app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_active_stock_reservations,
    )

    active_id, product_id = _materialize(pg_app)
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def reader():
        try:
            barrier.wait(10)
            with pg_app.app_context():
                found = [o.id for o in find_active_stock_reservations()]
                unproven = count_unproven_stock_reservations()
                results.append((found, unproven))
        except Exception as exc:  # noqa: BLE001 - prova que nada travou
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert len(results) == 8
    assert all(found == [active_id] and unproven == 1 for found, unproven in results)

    with pg_app.app_context():
        assert Order.query.count() == 5
        assert OrderItem.query.count() == 5
        assert db.session.get(Product, product_id).stock == 9
        assert db.session.get(Order, active_id).stock_reserved is True
