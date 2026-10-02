"""Item 1.3 — migration 0032 (auditoria da saída de manual_review) em PG.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_mr_decision_* criado e dropado; NUNCA toca public nem produção.

Cobre em PostgreSQL (o dialeto real de produção):
- UP 0032: 4 colunas + CHECK ck_orders_manual_review_decision + FK do
  admin + índice, tudo idempotente (reaplicar não quebra);
- o CHECK rejeita valores fora de {'approved','rejected'} e aceita o par;
- DOWN 0032: remove constraint/FK/colunas; UP reexecuta limpo.
"""

import secrets
import uuid
from decimal import Decimal
from pathlib import Path

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

UP = "0032_manual_review_decision.sql"
DOWN = "0032_manual_review_decision_down.sql"

COLUMNS = (
    "manual_review_decision",
    "manual_review_decided_at",
    "manual_review_decided_by_user_id",
    "manual_review_decided_reason",
)


@pytest.fixture
def pg_env(monkeypatch):
    schema = "pollo_test_mr_decision_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "mr-decision-pg")
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

    yield app, engine, schema

    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _migration_sql(filename, schema):
    return (
        Path("migrations", filename)
        .read_text()
        .replace("pollomodas.", '"%s".' % schema)
    )


def _run_migration(engine, schema, filename):
    with engine.begin() as connection:
        connection.exec_driver_sql(_migration_sql(filename, schema))


def _columns(engine, schema):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'orders'"
            ),
            {"s": schema},
        ).all()
    return {r[0] for r in rows}


def _constraints(engine, schema):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t)"
            ),
            {"t": '"%s".orders' % schema},
        ).all()
    return {r[0] for r in rows}


def _indexes(engine, schema):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = :s AND tablename = 'orders'"
            ),
            {"s": schema},
        ).all()
    return {r[0] for r in rows}


def _seed_order(app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import User

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG MR Decision",
            email="mr-%s@pg.test" % uuid.uuid4().hex,
            is_admin=True,
        )
        user.set_password("senha123")
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="manual_review",
            manual_review_reason="Valor/moeda divergente.",
            subtotal=Decimal("20.00"),
            shipping=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("30.00"),
            stock_reserved=False,
            customer_name="Cliente MR",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua sintética",
            number="10",
            neighborhood="Centro",
            city="São Paulo",
            state="SP",
        )
        db.session.add_all([user, order])
        db.session.commit()
        return order.id


def _lit_uuid(value):
    return "'%s'::uuid" % value


def test_migration_0032_adds_audit_columns_idempotent(pg_env):
    app, engine, schema = pg_env
    order_id = _seed_order(app)

    _run_migration(engine, schema, UP)
    cols = _columns(engine, schema)
    for column in COLUMNS:
        assert column in cols
    constraints = _constraints(engine, schema)
    assert "ck_orders_manual_review_decision" in constraints
    assert "orders_manual_review_decided_by_user_id_fkey" in constraints
    assert "ix_orders_manual_review_decided_by_user_id" in _indexes(
        engine, schema
    )

    # CHECK: valor desconhecido recusado; par canônico aceito.
    with pytest.raises(Exception):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                'UPDATE "%s".orders SET manual_review_decision = %s '
                "WHERE id = %s" % (schema, "'maybe'", _lit_uuid(order_id))
            )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'UPDATE "%s".orders SET manual_review_decision = %s, '
            "manual_review_decided_at = now(), "
            "manual_review_decided_reason = 'ok' "
            "WHERE id = %s"
            % (schema, "'approved'", _lit_uuid(order_id))
        )

    # Idempotente: reaplicar o UP não falha nem perde dado auditado.
    _run_migration(engine, schema, UP)
    _run_migration(engine, schema, UP)
    with engine.begin() as connection:
        value = connection.exec_driver_sql(
            'SELECT manual_review_decision FROM "%s".orders WHERE id = %s'
            % (schema, _lit_uuid(order_id))
        ).scalar()
    assert value == "approved"


def test_migration_0032_down_removes_and_up_restores(pg_env):
    app, engine, schema = pg_env
    _seed_order(app)
    _run_migration(engine, schema, UP)

    _run_migration(engine, schema, DOWN)
    cols = _columns(engine, schema)
    for column in COLUMNS:
        assert column not in cols
    constraints = _constraints(engine, schema)
    assert "ck_orders_manual_review_decision" not in constraints
    assert "orders_manual_review_decided_by_user_id_fkey" not in constraints

    # Recuperar: UP reexecuta limpo após o DOWN.
    _run_migration(engine, schema, UP)
    cols = _columns(engine, schema)
    for column in COLUMNS:
        assert column in cols
    assert "ck_orders_manual_review_decision" in _constraints(engine, schema)
