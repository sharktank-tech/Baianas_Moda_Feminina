"""Compra manual + despacho em PostgreSQL real (sem skips no CI).

1. Migration 0039 aplica limpa (idempotente) e o validador passa.
2. Registro concorrente do MESMO número externo: exatamente uma linha;
   o perdedor recebe erro limpo de duplicidade (sem duplicar).
3. Mesmo código de rastreio em corrida: exatamente uma remessa.
Somente banco descartável loopback com nome pollo*.
"""
from __future__ import annotations

import secrets
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

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

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def pg_app(monkeypatch):
    schema = "pollo_test_manual_dispatch_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "manual-dispatch-pg")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {
                "options": "-csearch_path=%s -cstatement_timeout=60000" % schema
            },
        },
    )
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", schema)

    from loja import create_app

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app.config["TEST_SCHEMA"] = schema
    with app.app_context():
        from loja import db

        db.create_all()

    yield app

    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def test_migration_0039_aplica_e_valida(pg_app):
    from migrations.validate_0039 import validate_schema

    schema = pg_app.config["TEST_SCHEMA"]
    engine = create_engine(
        DATABASE_URL,
        connect_args={"options": "-csearch_path=%s" % schema},
    )
    with engine.begin() as conn:
        assert conn.execute(text("SELECT current_schema()")).scalar() == schema
        migration = Path("migrations/0039_manual_dispatch.sql").read_text()
        migration = migration.replace("SET search_path TO pollomodas;",
                                      'SET search_path TO "%s";' % schema)
        lines = [ln for ln in migration.splitlines()
                 if not ln.lstrip().startswith("\\")]
        for _ in (1, 2):  # idempotente: aplica 2x
            conn.exec_driver_sql("\n".join(lines))
    engine.dispose()

    import psycopg2

    raw = psycopg2.connect(
        DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://"),
        connect_timeout=15,
    )
    raw.autocommit = True
    errors = validate_schema(raw, schema=schema)
    raw.close()
    assert errors == []


def _materialize(app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import (
        Order, OrderItem, Payment,
    )
    from loja.modules.models import Product, User

    with app.app_context():
        user = User(
            id=uuid.uuid4(), name="PG Manual", is_admin=True,
            email="manual-%s@pg.test" % uuid.uuid4().hex,
        )
        user.set_password("senha123")
        product = Product(
            id=uuid.uuid4(), name="Vestido PG", description="d",
            price=Decimal("49.90"), image1="https://exemplo.com/i.png",
            category="feminino", stock=10, is_active=True,
        )
        db.session.add_all([user, product])
        db.session.flush()
        order = Order(
            id=uuid.uuid4(), user_id=user.id, checkout_type="product",
            status="paid", subtotal=Decimal("100.00"),
            shipping=Decimal("10.00"), discount=Decimal("0.00"),
            total=Decimal("110.00"), customer_name="Cliente",
            customer_email=user.email, zip_code="77066520", street="Rua",
            number="1", neighborhood="C", city="Palmas", state="TO",
            mp_payment_id=uuid.uuid4().hex, mp_status="approved",
            paid_at=NOW,
        )
        db.session.add(order)
        db.session.flush()
        item = OrderItem(
            id=uuid.uuid4(), order_id=order.id, product_id=product.id,
            product_name=product.name, quantity=2,
            unit_price=product.price, line_total=product.price * 2,
        )
        payment = Payment(
            id=uuid.uuid4(), provider="mercado_pago",
            provider_payment_id=order.mp_payment_id, status="approved",
            amount=order.total, currency="BRL", raw_payload={},
        )
        order.payments.append(payment)
        db.session.add(item)
        db.session.commit()
        return {"order_id": order.id, "item_id": item.id,
                "user_id": user.id}


def _race(pg_app, ids, call):
    from loja import db

    gate = threading.Barrier(3)
    errors, results = [], []

    def worker(tag):
        with pg_app.app_context():
            try:
                gate.wait(30)
                results.append((tag, call()))
                db.session.commit()
            except Exception as exc:  # noqa: BLE001 (duplicata esperada)
                db.session.rollback()
                errors.append((tag, type(exc).__name__, str(exc)[:200]))
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(2)]
    for thread in threads:
        thread.start()
    gate.wait(30)
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    return results, errors


def test_corrida_mesmo_numero_externo_uma_linha(pg_app):
    from loja import db
    from loja.modules.models import ManualSupplierPurchase
    from loja.services import manual_dispatch as dispatch

    ids = _materialize(pg_app)

    def call():
        return dispatch.register_manual_purchase(
            order_id=ids["order_id"], external_order_id="ALI-RACE-1",
            items={ids["item_id"]: 2}, purchased_at="2026-10-08T10:00:00",
            purchased_tz="America/Sao_Paulo", evidence_ref="EV",
            admin_user_id=ids["user_id"],
        )

    results, errors = _race(pg_app, ids, call)
    with pg_app.app_context():
        total = ManualSupplierPurchase.query.count()
    assert total == 1
    assert len(results) + len(errors) == 2
    # Perdedor: erro limpo de duplicidade (constraint ou pré-checagem).
    for _, kind, _ in errors:
        assert kind in ("IntegrityError", "ValueError")


def test_corrida_mesmo_rastreio_uma_remessa(pg_app):
    from loja import db
    from loja.modules.models import ManualSupplierPurchase, Shipment
    from loja.services import manual_dispatch as dispatch

    ids = _materialize(pg_app)
    with pg_app.app_context():
        purchase = dispatch.register_manual_purchase(
            order_id=ids["order_id"], external_order_id="ALI-RACE-2",
            items={ids["item_id"]: 2}, purchased_at="2026-10-08T10:00:00",
            purchased_tz="America/Sao_Paulo", evidence_ref="EV",
            admin_user_id=ids["user_id"],
        )
        db.session.commit()
        purchase_id = purchase.purchase_id

    def call():
        return dispatch.register_manual_shipment(
            order_id=ids["order_id"], purchase_id=purchase_id,
            tracking_code="BR999999999XX", carrier="Correios",
            evidence_ref="EV", items={ids["item_id"]: 1},
        )

    results, errors = _race(pg_app, ids, call)
    with pg_app.app_context():
        total = Shipment.query.filter_by(tracking_code="BR999999999XX").count()
    assert total == 1
    assert len(results) + len(errors) == 2
    for _, kind, _ in errors:
        assert kind in ("IntegrityError", "ValueError")
