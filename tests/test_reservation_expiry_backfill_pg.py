"""Item 1.1: backfill do prazo de expiração da reserva em PostgreSQL.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_exp_backfill_* criado e dropado; NUNCA toca public nem produção.

Cobre em PostgreSQL (o dialeto real de produção):

- migration 0030 aplicada 2x (idempotente) e DOWN reversível: somente
  pending_payment com prazo NULL são preenchidos com created_at + 24h;
  controles (pago/cancelado NULL, pendente com prazo do código) ficam
  intocados; o DOWN reverte só linhas anteriores à 0024 (corte documentado);
- integração com o mecanismo existente: ANTES do backfill os legados contam
  em count_unproven_stock_reservations e não aparecem em
  find_expired_stock_reservations (falha fechada — estoque travado para
  sempre); DEPOIS do backfill eles expiram, o cron/liberação devolve o
  estoque do pedido sem pagamento aprovado e IGNORA o que tem pagamento
  aprovado (guarda fail-closed intacta), sem mudar o status do pedido
  (design: liberação não cancela pedidos).

A versão SQLite (guarda contra NOVAS linhas NULL: o endpoint de checkout
grava o prazo) fica em tests/test_reservation_expiry_backfill.py.
"""

import secrets
import uuid
from datetime import timedelta
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


@pytest.fixture
def pg_env(monkeypatch):
    schema = "pollo_test_exp_backfill_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "exp-backfill-pg")
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


LEGACY_AGO = timedelta(days=12)


def _make_order(user, product, *, status="pending_payment", reserved=True,
                created_ago=None, expires=None):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.utils import utcnow

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
        customer_name="Cliente backfill",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua sintética",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        created_at=utcnow() - created_ago if created_ago else utcnow(),
        mp_preference_expires_at=expires,
    )
    if reserved:
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
    return order


def _seed(app):
    """Cria o cenário legado pré-0024 + controles."""
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Payment
    from loja.modules.models import Product, User
    from loja.utils import utcnow

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Exp Backfill",
            email="exp-%s@pg.test" % uuid.uuid4().hex,
            is_admin=False,
        )
        user.set_password("senha123")

        product_a = Product(
            id=uuid.uuid4(),
            name="Produto A reserva",
            description="sintético",
            price=Decimal("20.00"),
            stock=4,
            source="manual",
            is_draft=True,
            is_active=False,
        )
        product_b = Product(
            id=uuid.uuid4(),
            name="Produto B reserva",
            description="sintético",
            price=Decimal("20.00"),
            stock=7,
            source="manual",
            is_draft=True,
            is_active=False,
        )

        legacy_pending = _make_order(
            user, product_a, created_ago=LEGACY_AGO, expires=None
        )
        legacy_paid = _make_order(
            user, product_a, status="paid", reserved=False,
            created_ago=LEGACY_AGO, expires=None,
        )
        legacy_cancelled = _make_order(
            user, product_a, status="cancelled", reserved=False,
            created_ago=LEGACY_AGO, expires=None,
        )
        paid_guard = _make_order(
            user, product_b, created_ago=LEGACY_AGO, expires=None
        )
        paid_guard.payments.append(
            Payment(
                id=uuid.uuid4(),
                order_id=paid_guard.id,
                provider_payment_id="exp-backfill-approved-%s"
                % uuid.uuid4().hex[:8],
                status="approved",
                amount=paid_guard.total,
                currency="BRL",
            )
        )
        not_reserved = _make_order(
            user, product_a, reserved=False,
            created_ago=LEGACY_AGO, expires=None,
        )
        fresh_with_expiry = _make_order(
            user, product_b,
            expires=utcnow() + timedelta(hours=24),
        )

        db.session.add_all(
            [
                user,
                product_a,
                product_b,
                legacy_pending,
                legacy_paid,
                legacy_cancelled,
                paid_guard,
                not_reserved,
                fresh_with_expiry,
            ]
        )
        db.session.commit()
        return {
            "legacy_pending": legacy_pending.id,
            "legacy_paid": legacy_paid.id,
            "legacy_cancelled": legacy_cancelled.id,
            "paid_guard": paid_guard.id,
            "not_reserved": not_reserved.id,
            "fresh_with_expiry": fresh_with_expiry.id,
            "product_a": product_a.id,
            "product_b": product_b.id,
        }


def _expires(app, order_id):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order

    with app.app_context():
        db.session.expire_all()
        return db.session.get(Order, order_id).mp_preference_expires_at


# ── Migration: escopo, idempotência, DOWN ────────────────────────────────────

def test_migration_0030_backfills_only_legacy_pending(pg_env):
    app, engine, schema = pg_env
    ids = _seed(app)
    from loja.modules.getway_mercado_pago.payment_models import Order

    up = "0030_reservation_expiry_backfill.sql"
    down = "0030_reservation_expiry_backfill_down.sql"

    # Pré-condição: legados pendentes NULL (o defeito do item 1.1).
    assert _expires(app, ids["legacy_pending"]) is None
    assert _expires(app, ids["paid_guard"]) is None

    _run_migration(engine, schema, up)

    with app.app_context():
        from loja import db

        db.session.expire_all()
        legacy_pending = db.session.get(Order, ids["legacy_pending"])
        assert legacy_pending.mp_preference_expires_at is not None
        assert (
            legacy_pending.mp_preference_expires_at
            == legacy_pending.created_at + timedelta(hours=24)
        )
        # Pendente com pagamento aprovado também é preenchido (dado puro;
        # a guarda contra pagamento fica na liberação).
        assert db.session.get(
            Order, ids["paid_guard"]
        ).mp_preference_expires_at is not None
        assert db.session.get(
            Order, ids["not_reserved"]
        ).mp_preference_expires_at is not None
        # Controles fora do WHERE ficam intactos.
        assert db.session.get(Order, ids["legacy_paid"]).mp_preference_expires_at is None
        assert (
            db.session.get(Order, ids["legacy_cancelled"]).mp_preference_expires_at
            is None
        )
        fresh = db.session.get(Order, ids["fresh_with_expiry"])
        assert fresh.mp_preference_expires_at is not None
        assert fresh.created_at > legacy_pending.created_at  # criado agora

    # Idempotente: reaplicar não muda nada.
    before = _snapshot(engine, schema, ids)
    _run_migration(engine, schema, up)
    assert _snapshot(engine, schema, ids) == before

    # DOWN: reverte só as linhas anteriores à 0024 (assinatura created+24h);
    # a linha criada agora (prazo vindo do código) é preservada.
    _run_migration(engine, schema, down)
    assert _expires(app, ids["legacy_pending"]) is None
    assert _expires(app, ids["paid_guard"]) is None
    assert _expires(app, ids["fresh_with_expiry"]) is not None

    # Recuperar: reaplicar o UP restaura o backfill (idempotente).
    _run_migration(engine, schema, up)
    with app.app_context():
        from loja import db

        db.session.expire_all()
        legacy_pending = db.session.get(Order, ids["legacy_pending"])
        assert (
            legacy_pending.mp_preference_expires_at
            == legacy_pending.created_at + timedelta(hours=24)
        )


def _snapshot(engine, schema, ids):
    values = {}
    with engine.begin() as conn:
        for key in (
            "legacy_pending",
            "legacy_paid",
            "legacy_cancelled",
            "paid_guard",
            "not_reserved",
            "fresh_with_expiry",
        ):
            row = conn.exec_driver_sql(
                'SELECT mp_preference_expires_at FROM "%s"."orders" '
                "WHERE id = %%s" % schema,
                (ids[key],),
            ).fetchone()
            values[key] = row[0]
    return values


# ── Integração: backfill → expiração → liberação ─────────────────────────────

def test_backfill_unblocks_expiry_and_release(pg_env):
    app, engine, schema = pg_env
    ids = _seed(app)

    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_expired_stock_reservations,
        release_expired_stock_reservations,
    )

    with app.app_context():
        # Antes: legado NULL sem comprovação = estoque travado para sempre
        # (falha fechada). O legado com pagamento aprovado sai da contagem
        # (pagamento decide), mas também tem prazo NULL — alvo do backfill.
        assert count_unproven_stock_reservations() == 1
        assert find_expired_stock_reservations() == []
        product_a = db.session.get(Product, ids["product_a"])
        assert product_a.stock == 4

    _run_migration(engine, schema, "0030_reservation_expiry_backfill.sql")

    with app.app_context():
        db.session.expire_all()
        # Depois: nada mais "unproven"; os legados vencidos aparecem.
        # (A listagem já filtra pagamento aprovado — "pagamento decide" —
        # por isso o guard não vem em find_expired; na liberação o pagamento
        # é relido sob lock e vira skipped.)
        assert count_unproven_stock_reservations() == 0
        candidates = find_expired_stock_reservations()
        candidate_ids = {row.id for row in candidates}
        assert ids["legacy_pending"] in candidate_ids
        assert ids["paid_guard"] not in candidate_ids
        assert ids["fresh_with_expiry"] not in candidate_ids

        result = release_expired_stock_reservations(source="pg_backfill_test")
        assert result.candidates == 2
        assert result.released == 1
        assert result.skipped == 1

        db.session.expire_all()
        released_order = db.session.get(Order, ids["legacy_pending"])
        # Estoque devolvido; status NÃO muda (design: não cancela pedidos).
        assert released_order.stock_reserved is False
        assert released_order.status == "pending_payment"
        product_a = db.session.get(Product, ids["product_a"])
        assert product_a.stock == 5

        # Guarda fail-closed intacta: pagamento aprovado → sem liberação.
        guard_order = db.session.get(Order, ids["paid_guard"])
        assert guard_order.stock_reserved is True
        product_b = db.session.get(Product, ids["product_b"])
        assert product_b.stock == 7
