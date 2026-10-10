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
    """Parte de schema legado: aplica 2x, valida tipos/constraints/índices.

    Usa engine/schema próprios SEM create_all: prova atualização real do
    schema anterior (não no-op sobre tabelas já criadas pelos models).
    """
    from migrations.validate_0039 import validate_schema

    schema = "pollo_test_manual_mig_%s" % secrets.token_hex(4)
    engine = create_engine(
        DATABASE_URL,
        connect_args={"options": "-csearch_path=%s" % schema},
    )
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE SCHEMA "%s"' % schema))
            for stmt in (
                "CREATE TABLE users (id uuid PRIMARY KEY)",
                "CREATE TABLE orders (id uuid PRIMARY KEY)",
                "CREATE TABLE order_items (id uuid PRIMARY KEY, "
                "order_id uuid NOT NULL REFERENCES orders(id))",
                "CREATE TABLE shipments (id bigint PRIMARY KEY, "
                "order_id uuid NOT NULL REFERENCES orders(id), "
                "tracking_code text UNIQUE NOT NULL, carrier text, "
                "provider text NOT NULL DEFAULT 'manual', "
                "status text NOT NULL DEFAULT 'aguardando_postagem')",
            ):
                conn.execute(text(stmt))
            seed_order = uuid.uuid4()
            conn.execute(
                text("INSERT INTO orders (id) VALUES (:id)"),
                {"id": str(seed_order)},
            )
            conn.execute(
                text("INSERT INTO shipments (id, order_id, tracking_code) "
                     "VALUES (7, :id, 'BR000000007XX')"),
                {"id": str(seed_order)},
            )
            migration = Path(
                "migrations/0039_manual_dispatch.sql").read_text()
            migration = migration.replace(
                "SET search_path TO pollomodas;",
                'SET search_path TO "%s";' % schema,
            )
            runnable = "\n".join(
                ln for ln in migration.splitlines()
                if not ln.lstrip().startswith("\\")
            )
            for _ in (1, 2):  # idempotente: aplica 2x
                conn.exec_driver_sql(runnable)
            # Registros legados preservados.
            assert conn.execute(
                text("SELECT tracking_code FROM shipments WHERE id = 7")
            ).scalar() == "BR000000007XX"
            assert conn.execute(
                text("SELECT count(*) FROM orders")).scalar() == 1

        import psycopg2

        raw = psycopg2.connect(
            DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://"),
            connect_timeout=15,
        )
        raw.autocommit = True
        try:
            from migrations.validate_0039 import validate_schema

            assert validate_schema(raw, schema=schema) == []
        finally:
            raw.close()
    finally:
        with engine.begin() as conn:
            conn.execute(
                text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
        engine.dispose()
        engine.dispose()


def test_validate_0039_recusa_estrutura_incorreta():
    """Controles negativos: estrutura errada gera erros específicos."""
    from migrations.validate_0039 import validate_schema

    schema = "pollo_test_manual_neg_%s" % secrets.token_hex(4)
    engine = create_engine(
        DATABASE_URL,
        connect_args={"options": "-csearch_path=%s" % schema},
    )
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE SCHEMA "%s"' % schema))
            # Falhas propositais: sem coluna note, tipo errado em quantity,
            # sem UNIQUE no número externo, sem checks/índices/FKs, sem tabelas.
            conn.execute(text(
                "CREATE TABLE manual_supplier_purchases ("
                "id uuid PRIMARY KEY, order_id uuid NOT NULL, "
                "external_order_id varchar(64) NOT NULL, "
                "purchased_at timestamp NOT NULL, "
                "purchased_tz varchar(64) NOT NULL, "
                "evidence_ref varchar(64) NOT NULL, "
                "created_by uuid, created_at timestamp NOT NULL, "
                "updated_at timestamp NOT NULL)"
            ))
            conn.execute(text(
                "CREATE TABLE manual_supplier_purchase_items ("
                "id uuid PRIMARY KEY, purchase_id uuid NOT NULL, "
                "order_item_id uuid NOT NULL, quantity text NOT NULL)"
            ))
        import psycopg2

        raw = psycopg2.connect(
            DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://"),
            connect_timeout=15,
        )
        raw.autocommit = True
        try:
            errors = validate_schema(raw, schema=schema)
        finally:
            raw.close()
        assert len(errors) >= 4, errors
        joined = "\n".join(errors)
        assert "manual_supplier_purchases" in joined
        assert "manual_supplier_purchase_items" in joined
        assert "shipment_items" in joined
        assert "manual_operational_audit" in joined
        assert "FK ausente" in joined
    finally:
        with engine.begin() as conn:
            conn.execute(
                text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
        engine.dispose()


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
    """Duas threads largando juntas da barreira; retorna tudo + eventos."""
    from loja import db

    gate = threading.Barrier(3)
    errors, results = [], []
    entered = [threading.Event(), threading.Event()]
    done = [threading.Event(), threading.Event()]

    def worker(tag):
        with pg_app.app_context():
            try:
                gate.wait(30)
                entered[tag].set()
                results.append((tag, call()))
                db.session.commit()
                done[tag].set()
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
    return threads, entered, done, results, errors


def _join_all(threads):
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)


def test_corrida_mesmo_numero_externo_uma_linha(pg_app, monkeypatch):
    """Sobreposição real: a 2ª thread espera o lock da 1ª; 1 linha + totais.

    Pausa a 1ª chamada DENTRO da seção crítica (contabilidade sob o lock
    de Order); a 2ª larga junto da barreira e não termina enquanto o lock
    está segurado — evidência de contenção, não execução serial.
    """
    from loja import db
    from loja.modules.models import (
        ManualSupplierPurchase, ManualSupplierPurchaseItem,
    )
    from loja.services import manual_dispatch as dispatch

    ids = _materialize(pg_app)
    holding = threading.Event()
    allow = threading.Event()
    paused = threading.Event()
    original_sum = dispatch._sum_registered

    def pausing_sum(order_item_id, *, exclude_purchase_id=None):
        if not paused.is_set():
            paused.set()
            holding.set()
            assert allow.wait(60)
        return original_sum(
            order_item_id, exclude_purchase_id=exclude_purchase_id
        )

    monkeypatch.setattr(dispatch, "_sum_registered", pausing_sum)

    def call():
        return dispatch.register_manual_purchase(
            order_id=ids["order_id"], external_order_id="ALI-RACE-1",
            items={ids["item_id"]: 2}, purchased_at="2026-10-08T10:00:00",
            purchased_tz="America/Sao_Paulo", evidence_ref="EV",
            admin_user_id=ids["user_id"],
        )

    threads, entered, done, results, errors = _race(pg_app, ids, call)
    assert holding.wait(30)
    assert entered[0].wait(15) and entered[1].wait(15)
    assert done[0].wait(3) is False and done[1].wait(3) is False
    allow.set()
    _join_all(threads)

    assert errors == [] or all(k == "ValueError" for _, k, _ in errors)
    with pg_app.app_context():
        assert ManualSupplierPurchase.query.count() == 1
        purchase = ManualSupplierPurchase.query.first()
        assert purchase.external_order_id == "ALI-RACE-1"
        linked = sum(
            link.quantity for link in purchase.items
        )
        assert linked == 2  # quantidade final exata, sem duplicar nem perder
        assert ManualSupplierPurchaseItem.query.count() == 1


def test_corrida_mesmo_rastreio_uma_remessa(pg_app, monkeypatch):
    """Mesmo código em corrida: sobreposição sob lock, 1 remessa idêntica.

    A repetição idêntica (mesma compra + mesmos itens) retorna o registro
    existente; cobertura final exata, sem duplicar.
    """
    from loja import db
    from loja.modules.models import Shipment, ShipmentItem
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

    holding = threading.Event()
    allow = threading.Event()
    paused = threading.Event()
    original_covered = dispatch._sum_covered

    def pausing_covered(purchase_id_, order_item_id, *, exclude_shipment_id=None):
        if not paused.is_set():
            paused.set()
            holding.set()
            assert allow.wait(60)
        return original_covered(
            purchase_id_, order_item_id,
            exclude_shipment_id=exclude_shipment_id,
        )

    monkeypatch.setattr(dispatch, "_sum_covered", pausing_covered)

    def call():
        return dispatch.register_manual_shipment(
            order_id=ids["order_id"], purchase_id=purchase_id,
            tracking_code="BR999999999XX", carrier="Correios",
            evidence_ref="EV", items={ids["item_id"]: 1},
        )

    threads, entered, done, results, errors = _race(pg_app, ids, call)
    assert holding.wait(30)
    assert entered[0].wait(15) and entered[1].wait(15)
    assert done[0].wait(3) is False and done[1].wait(3) is False
    allow.set()
    _join_all(threads)

    assert errors == []
    assert sorted(r.created for _, r in results) == [False, True]
    with pg_app.app_context():
        shipments = Shipment.query.filter_by(
            tracking_code="BR999999999XX").all()
        assert len(shipments) == 1
        assert shipments[0].status == "aguardando_postagem"
        assert sum(
            link.quantity for link in ShipmentItem.query.filter_by(
                shipment_id=shipments[0].id).all()
        ) == 1


def _paused_race(pg_app, monkeypatch, hook_target, calls):
    """Duas threads com identificadores diferentes largando juntas.

    Pausa a 1ª dentro da seção crítica (sob lock); a 2ª não termina
    enquanto o lock está segurado. Retorna (threads, entered, done).
    """
    import loja.services.manual_dispatch as dispatch

    holding = threading.Event()
    allow = threading.Event()
    paused = threading.Event()
    original = hook_target

    def pausing(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            holding.set()
            assert allow.wait(60)
        return original(*args, **kwargs)

    from loja import db

    gate = threading.Barrier(3)
    errors, results = [], []
    entered = [threading.Event(), threading.Event()]
    done = [threading.Event(), threading.Event()]

    def worker(tag):
        with pg_app.app_context():
            try:
                gate.wait(30)
                entered[tag].set()
                results.append((tag, calls[tag]()))
                db.session.commit()
                done[tag].set()
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                errors.append((tag, type(exc).__name__, str(exc)[:200]))
            finally:
                db.session.remove()

    monkeypatch.setattr(dispatch, original.__name__, pausing)
    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(2)]
    for thread in threads:
        thread.start()
    gate.wait(30)
    assert holding.wait(30)
    assert entered[0].wait(15) and entered[1].wait(15)
    assert done[0].wait(3) is False and done[1].wait(3) is False
    allow.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    return results, errors


def test_corrida_saldo_compra_somente_permitido_persiste(pg_app, monkeypatch):
    """ALI-X e ALI-Y disputam 2 unidades: soma final == 2, sem parcial."""
    from loja import db
    from loja.modules.models import (
        ManualSupplierPurchase, ManualSupplierPurchaseItem,
    )
    from loja.services import manual_dispatch as dispatch

    ids = _materialize(pg_app)

    def make_call(code):
        def call():
            return dispatch.register_manual_purchase(
                order_id=ids["order_id"], external_order_id=code,
                items={ids["item_id"]: 2}, purchased_at="2026-10-08T10:00:00",
                purchased_tz="America/Sao_Paulo", evidence_ref="EV",
                admin_user_id=ids["user_id"],
            )
        return call

    results, errors = _paused_race(
        pg_app, monkeypatch, dispatch._sum_registered,
        [make_call("ALI-X"), make_call("ALI-Y")],
    )
    assert len(results) == 1 and len(errors) == 1
    assert errors[0][1] == "ValueError" and "quantity_exceeds_sold" in errors[0][2]
    with pg_app.app_context():
        purchases = ManualSupplierPurchase.query.all()
        assert len(purchases) == 1  # perdedor não deixou linha parcial
        total = sum(
            link.quantity for link in
            ManualSupplierPurchaseItem.query.all()
        )
        assert total == 2  # exatamente o permitido


def test_corrida_saldo_rastreio_somente_permitido_persiste(pg_app, monkeypatch):
    """CODE-A e CODE-B disputam cobertura 2: soma final == 2, sem parcial."""
    from loja import db
    from loja.modules.models import Shipment, ShipmentItem
    from loja.services import manual_dispatch as dispatch

    ids = _materialize(pg_app)
    with pg_app.app_context():
        purchase = dispatch.register_manual_purchase(
            order_id=ids["order_id"], external_order_id="ALI-SALDO",
            items={ids["item_id"]: 2}, purchased_at="2026-10-08T10:00:00",
            purchased_tz="America/Sao_Paulo", evidence_ref="EV",
            admin_user_id=ids["user_id"],
        )
        db.session.commit()
        purchase_id = purchase.purchase_id

    def make_call(code):
        def call():
            return dispatch.register_manual_shipment(
                order_id=ids["order_id"], purchase_id=purchase_id,
                tracking_code=code, carrier="Correios",
                evidence_ref="EV", items={ids["item_id"]: 2},
            )
        return call

    results, errors = _paused_race(
        pg_app, monkeypatch, dispatch._sum_covered,
        [make_call("BRAAAAAAAAAAAAAAA1X"), make_call("BRBBBBBBBBBBBBBB2X")],
    )
    assert len(results) == 1 and len(errors) == 1
    assert errors[0][1] == "ValueError"
    assert "quantity_exceeds_purchase" in errors[0][2]
    with pg_app.app_context():
        assert Shipment.query.count() == 1
        assert sum(link.quantity for link in ShipmentItem.query.all()) == 2
