"""Item 1.1 (fechamento): backfill do estado `expired` em PostgreSQL.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_expired_state_* criado e dropado; NUNCA toca public nem produção.

Cobre em PostgreSQL (o dialeto real de produção):

- migration 0031 aplicada 2x (idempotente) e DOWN reversível: só vira
  `expired` o pedido com reserva JÁ devolvida (stock_reserved=false), prazo
  preenchido e vencido e NENHUM pagamento aprovado;
- controles permanecem intocados: pagamento aprovado (qualquer linha
  `approved`, mesmo que não seja a mais recente), prazo NULL, reserva ainda
  ativa, prazo futuro e status que não seja `pending_payment`;
- a reserva ativa vencida fica com o cron/liberação do código #90 (lock por
  linha + revalidação) — o backfill não duplica esse papel.

A versão SQLite do ciclo de vida do estado `expired` (liberação ->
transição, webhook tardio) fica em tests/test_order_expired_state.py.
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
    schema = "pollo_test_expired_state_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "expired-state-pg")
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
        cursor = connection.exec_driver_sql(_migration_sql(filename, schema))
        return cursor.rowcount


def _make_order(
    user,
    product,
    *,
    status="pending_payment",
    reserved=True,
    expires=None,
):
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
        customer_name="Cliente Backfill Estado",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua sintética",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        created_at=utcnow() - timedelta(days=11),
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
    """Cenário: liberados pré-#90 (o defeito) + controles fail-closed."""
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Payment
    from loja.modules.models import Product, User
    from loja.utils import utcnow

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Expired State",
            email="expired-state-%s@pg.test" % uuid.uuid4().hex,
            is_admin=False,
        )
        user.set_password("senha123")

        product = Product(
            id=uuid.uuid4(),
            name="Produto Estado Expirado",
            description="sintético",
            price=Decimal("20.00"),
            stock=6,
            source="manual",
            is_draft=True,
            is_active=False,
        )

        past = utcnow() - timedelta(days=2)
        future = utcnow() + timedelta(days=2)

        # O defeito: reserva devolvida antes do estado existir.
        stuck_released = _make_order(user, product, reserved=False, expires=past)

        # Pagamento aprovado registrado → webhook decide, backfill não toca.
        approved_guard = _make_order(user, product, reserved=False, expires=past)
        approved_guard.payments.append(
            Payment(
                id=uuid.uuid4(),
                order_id=approved_guard.id,
                provider_payment_id="expired-state-approved-%s"
                % uuid.uuid4().hex[:8],
                status="approved",
                amount=approved_guard.total,
                currency="BRL",
            )
        )

        # Pagamento aprovado antigo + rejeição mais recente: gate
        # conservador (qualquer linha approved bloqueia o backfill).
        old_approved_guard = _make_order(
            user, product, reserved=False, expires=past
        )
        old_approved_guard.payments.append(
            Payment(
                id=uuid.uuid4(),
                order_id=old_approved_guard.id,
                provider_payment_id="expired-state-old-approved-%s"
                % uuid.uuid4().hex[:8],
                status="approved",
                amount=old_approved_guard.total,
                currency="BRL",
            )
        )
        old_approved_guard.payments.append(
            Payment(
                id=uuid.uuid4(),
                order_id=old_approved_guard.id,
                provider_payment_id="expired-state-rejected-%s"
                % uuid.uuid4().hex[:8],
                status="rejected",
                amount=old_approved_guard.total,
                currency="BRL",
            )
        )

        # Sem prova de expiração (prazo NULL) → intocado.
        no_prazo = _make_order(user, product, reserved=False, expires=None)

        # Reserva ainda ativa → cron/código #90 cuida, não o backfill.
        still_reserved = _make_order(user, product, reserved=True, expires=past)

        # Prazo futuro → ainda aguardando pagamento normalmente.
        fresh = _make_order(user, product, reserved=False, expires=future)

        # Status fora de pending_payment → fora do WHERE.
        paid_past = _make_order(
            user, product, status="paid", reserved=False, expires=past
        )

        db.session.add_all(
            [
                user,
                product,
                stuck_released,
                approved_guard,
                old_approved_guard,
                no_prazo,
                still_reserved,
                fresh,
                paid_past,
            ]
        )
        db.session.commit()
        return {
            "stuck_released": stuck_released.id,
            "approved_guard": approved_guard.id,
            "old_approved_guard": old_approved_guard.id,
            "no_prazo": no_prazo.id,
            "still_reserved": still_reserved.id,
            "fresh": fresh.id,
            "paid_past": paid_past.id,
        }


def _statuses(app, ids):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order

    with app.app_context():
        db.session.expire_all()
        return {
            key: db.session.get(Order, order_id).status
            for key, order_id in ids.items()
        }


UP = "0031_expired_state_backfill.sql"
DOWN = "0031_expired_state_backfill_down.sql"

EXPECTED_AFTER_UP = {
    "stuck_released": "expired",
    "approved_guard": "pending_payment",
    "old_approved_guard": "pending_payment",
    "no_prazo": "pending_payment",
    "still_reserved": "pending_payment",
    "fresh": "pending_payment",
    "paid_past": "paid",
}

EXPECTED_AFTER_DOWN = {
    "stuck_released": "pending_payment",
    "approved_guard": "pending_payment",
    "old_approved_guard": "pending_payment",
    "no_prazo": "pending_payment",
    "still_reserved": "pending_payment",
    "fresh": "pending_payment",
    "paid_past": "paid",
}


def test_migration_0031_flips_only_released_expired_unpaid(pg_env):
    app, engine, schema = pg_env
    ids = _seed(app)

    # Pré-condição: o caso defeituoso ainda está travado em pending_payment.
    assert _statuses(app, ids)["stuck_released"] == "pending_payment"

    # UP: só o liberado-sem-pagamento vira expired.
    assert _run_migration(engine, schema, UP) == 1
    assert _statuses(app, ids) == EXPECTED_AFTER_UP

    # Idempotente: reaplicar não altera nenhuma linha.
    assert _run_migration(engine, schema, UP) == 0
    assert _statuses(app, ids) == EXPECTED_AFTER_UP

    # DOWN: reverte somente a linha que o backfill virou.
    assert _run_migration(engine, schema, DOWN) == 1
    assert _statuses(app, ids) == EXPECTED_AFTER_DOWN

    # Recuperar: reaplicar o UP restaura o estado expirado.
    assert _run_migration(engine, schema, UP) == 1
    assert _statuses(app, ids) == EXPECTED_AFTER_UP


def test_migration_0031_reserved_expired_belongs_to_cron_not_backfill(pg_env):
    """Reserva ativa vencida fica com o mecanismo de liberação (código #90)."""
    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    app, engine, schema = pg_env
    ids = _seed(app)

    assert _run_migration(engine, schema, UP) == 1
    statuses = _statuses(app, ids)
    assert statuses["still_reserved"] == "pending_payment"

    # O caminho normal (com lock) completa o que o backfill não faz.
    with app.app_context():
        from loja import db

        result = release_expired_stock_reservations(source="pg_backfill_test")
        assert result.released == 1

    statuses = _statuses(app, ids)
    assert statuses["still_reserved"] == "expired"
    # O resto permanece sob os gates do backfill.
    assert statuses["stuck_released"] == "expired"
    assert statuses["approved_guard"] == "pending_payment"
    assert statuses["no_prazo"] == "pending_payment"
    assert statuses["fresh"] == "pending_payment"
