"""Idempotência do checkout (item 1.7) em PostgreSQL — corrida real + migration.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_checkout_idem_* criado e dropado; NUNCA toca public nem produção.

Cobre em PostgreSQL (o dialeto real de produção):

- migration 0029 aplicada 2x (idempotente) e DOWN reversível: a coluna
  idempotency_key e o índice único (user_id, idempotency_key) existem e o
  2o INSERT com a mesma chave é recusado pelo banco;
- guard de boot (assert_checkout_idempotency_index): a aplicação RECUSA
  subir (RuntimeError visível, não silencioso) se a migration 0029 não
  estiver aplicada — ordem de deploy migration → release;
- duas requisições CONCORRENTES reais no endpoint POST
  /pagamentos/mercado-pago: o perdedor é observado BLOQUEADO em
  pg_stat_activity (wait_event_type=Lock) enquanto o vencedor ainda não
  commitou; depois do commit, o INSERT perdedor recebe a recusa da
  constraint UNIQUE e o cliente é redirecionado ao pedido do vencedor —
  1 pedido, 1 preferência, 1 débito de estoque, zero exceções.

A versão SQLite (com prova do caminho IntegrityError) fica em
tests/test_checkout_idempotency.py.
"""

import secrets
import threading
import time
import uuid
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

MP_URL_PREFIX = "https://mp.example/checkout"


@pytest.fixture
def pg_env(monkeypatch):
    schema = "pollo_test_checkout_idem_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "checkout-idem-pg")
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


def _product_form(user_email, product_id, key):
    return {
        "checkout_type": "product",
        "product_id": str(product_id),
        "quantity": "1",
        "selected_size": "M",
        "nome": "Cliente Teste",
        "email": user_email,
        "cpf": "52998224725",
        "cep": "01310100",
        "estado": "SP",
        "endereco": "Rua Teste",
        "numero": "10",
        "bairro": "Centro",
        "cidade": "São Paulo",
        "idempotency_key": key,
    }


def _login(client, email):
    return client.post(
        "/login",
        data={"email": email, "password": "senha123"},
    )


def _capture_app_logs(logger):
    import logging

    captured = []

    class Recorder(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Recorder()
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    def _stop():
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    return captured, _stop


def _materialize_user_product(app):
    from loja import db
    from loja.modules.models import Product, User

    email = "idem-%s@pg.test" % uuid.uuid4().hex[:10]
    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Checkout Idem",
            email=email,
            is_admin=False,
        )
        user.set_password("senha123")
        product = Product(
            id=uuid.uuid4(),
            name="Produto PG idempotencia",
            description="sintético",
            price=Decimal("30.00"),
            image1="https://exemplo.com/img.png",
            category="masculino",
            stock=5,
            source="manual",
            is_draft=False,
            is_active=True,
        )
        db.session.add_all([user, product])
        db.session.commit()
        return user.id, email, product.id


# ── Migration 0029 ───────────────────────────────────────────────────────────

def test_migration_0029_idempotent_and_enforces_unique(pg_env):
    """Item 1.7: a constraint é criada pela MIGRATION, não só pelo model.

    * 0029_down roda contra o schema criado pelo create_all (constraint de
      tabela) e remove coluna + índice;
    * 0029_up aplicado 2x é idempotente e deixa o índice único como em
      produção;
    * o banco (não o Python) recusa o 2o INSERT com a mesma
      (user_id, idempotency_key); NULL x NULL continua permitido.
    """
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import User

    app, engine, schema = pg_env
    up = _migration_sql("0029_order_checkout_idempotency.sql", schema)
    down = _migration_sql("0029_order_checkout_idempotency_down.sql", schema)

    # Estado legado simulado: remove o que o create_all criou.
    with engine.begin() as conn:
        conn.exec_driver_sql(down)
        present = conn.exec_driver_sql(
            f"SELECT count(*) FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = 'orders' "
            f"AND column_name = 'idempotency_key'"
        ).scalar()
        assert present == 0

    # UP duas vezes: idempotente, como o runbook de migrations exige.
    with engine.begin() as conn:
        conn.exec_driver_sql(up)
        conn.exec_driver_sql(up)
        indexdef = conn.exec_driver_sql(
            f"SELECT indexdef FROM pg_indexes "
            f"WHERE schemaname = '{schema}' "
            f"AND indexname = 'uq_orders_user_idempotency_key'"
        ).scalar()
    assert indexdef and "UNIQUE" in indexdef
    assert "(user_id, idempotency_key)" in indexdef

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Idem Migracao",
            email="mig-%s@pg.test" % uuid.uuid4().hex[:10],
            is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.flush()

        base = dict(
            user_id=user.id,
            checkout_type="product",
            status="pending_payment",
            subtotal=Decimal("30.00"),
            shipping=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("40.00"),
            stock_reserved=False,
            customer_name="Cliente PG",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua PG",
            number="10",
            neighborhood="Centro",
            city="São Paulo",
            state="SP",
        )
        key = "mig-key-1234567890"
        db.session.add(Order(id=uuid.uuid4(), idempotency_key=key, **base))
        db.session.commit()

        db.session.add(Order(id=uuid.uuid4(), idempotency_key=key, **base))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # NULL x NULL não colide (pedidos legados).
        db.session.add(Order(id=uuid.uuid4(), idempotency_key=None, **base))
        db.session.add(Order(id=uuid.uuid4(), idempotency_key=None, **base))
        db.session.commit()
        assert (
            db.session.query(Order).filter_by(user_id=user.id).count() == 3
        )


# ── Guard de boot: ordem de deploy (migration antes do release) ──────────────

def test_boot_guard_requires_unique_index_before_checkout(pg_env):
    """Item 1.7/3.3: o boot recusa subir sem o índice único da migration 0029.

    * create_all (constraint de tabela) → boot liberado;
    * DROP CONSTRAINT (forma legada removida) → RuntimeError visível com a
      instrução da migration 0029 (nunca falha silencioso);
    * CREATE UNIQUE INDEX (forma da migration, como em produção) → liberado;
    * DROP INDEX → RuntimeError de novo;
    * aplicar 0029 (idempotente) → liberado.
    """
    from loja.schema import (
        CHECKOUT_IDEMPOTENCY_INDEX,
        assert_checkout_idempotency_index,
    )

    app, engine, schema = pg_env

    # create_all criou a constraint → boot liberado.
    assert_checkout_idempotency_index(app)

    # Forma constraint de tabela: removida → boot recusado.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f'ALTER TABLE "{schema}".orders '
            f"DROP CONSTRAINT {CHECKOUT_IDEMPOTENCY_INDEX}"
        )
    with pytest.raises(RuntimeError, match="0029"):
        assert_checkout_idempotency_index(app)

    # Forma índice (migration 0029, como em produção) → boot liberado.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE UNIQUE INDEX {CHECKOUT_IDEMPOTENCY_INDEX} "
            f'ON "{schema}".orders (user_id, idempotency_key)'
        )
    assert_checkout_idempotency_index(app)

    # Removido → boot recusado de novo.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f'DROP INDEX "{schema}".{CHECKOUT_IDEMPOTENCY_INDEX}'
        )
    with pytest.raises(RuntimeError, match="0029"):
        assert_checkout_idempotency_index(app)

    # A migration 0029 up (idempotente) repara → boot liberado.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            _migration_sql("0029_order_checkout_idempotency.sql", schema)
        )
    assert_checkout_idempotency_index(app)


# ── Corrida real no endpoint ─────────────────────────────────────────────────

def test_concurrent_checkout_post_pg_single_order(pg_env, monkeypatch):
    """Item 1.7: 2 POSTs CONCORRENTES no endpoint -> 1 pedido em PostgreSQL.

    Sequência determinística:
    * o vencedor entra em create_preference com o flush feito e a transação
      ABERTA (row lock do produto + entrada do índice único não commitadas);
    * o perdedor só dispara depois disso e prova, no pre-check, que ainda
      não enxerga pedido (as duas requisições estão vivas ao mesmo tempo);
    * um observer (sessão separada) polla pg_stat_activity e só libera o
      commit do vencedor depois de observar o perdedor BLOQUEADO
      (wait_event_type=Lock) — evidência real de concorrência, sem sleep
      arbitrário como única prova;
    * depois do commit, o INSERT perdedor recebe a recusa da constraint
      UNIQUE, faz rollback da própria reserva e o cliente é redirecionado à
      MESMA URL do Mercado Pago do vencedor.
    """
    import loja.views.payments_routes as pr
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order

    app, engine, schema = pg_env

    # O estado da tabela vem da MIGRATION (como em produção), não do
    # create_all: DOWN + UP antes da corrida.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            _migration_sql("0029_order_checkout_idempotency_down.sql", schema)
        )
        conn.exec_driver_sql(
            _migration_sql("0029_order_checkout_idempotency.sql", schema)
        )

    user_id, user_email, product_id = _materialize_user_product(app)

    key = uuid.uuid4().hex
    calls = []
    gating_errors = []
    evidence = {}
    loser_pid = {}
    winner_in_txn = threading.Event()
    loser_missed_precheck = threading.Event()

    monkeypatch.setattr(
        pr, "quote_checkout_shipping",
        lambda item_data, cep: Decimal("10.00"),
    )

    original_find = pr._find_duplicate_checkout

    def tracked_find(candidate):
        result = original_find(candidate)
        if (
            threading.current_thread().name == "loser"
            and result is None
        ):
            loser_pid["pid"] = db.session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar()
            loser_missed_precheck.set()
        return result

    monkeypatch.setattr(pr, "_find_duplicate_checkout", tracked_find)

    def gated_preference(order):
        calls.append(order.id)
        if threading.current_thread().name != "winner":
            gating_errors.append(
                "create_preference chamado por thread errada"
            )
        winner_in_txn.set()
        if not loser_missed_precheck.wait(timeout=15):
            gating_errors.append(
                "perdedor não passou pelo pre-check com o vencedor "
                "sem commitar"
            )
        # Observer: só devolve quando o perdedor está bloqueado.
        observer = create_engine(DATABASE_URL)
        try:
            deadline = time.monotonic() + 15
            seen = None
            while time.monotonic() < deadline:
                pid = loser_pid.get("pid")
                if pid:
                    with observer.connect() as conn:
                        row = conn.execute(
                            text(
                                "SELECT wait_event_type, wait_event, "
                                "state FROM pg_stat_activity "
                                f"WHERE pid = {int(pid)}"
                            ),
                        ).fetchone()
                    if row and row[0] == "Lock":
                        seen = {
                            "wait_event_type": row[0],
                            "wait_event": row[1],
                            "state": row[2],
                        }
                        break
                time.sleep(0.05)
        finally:
            observer.dispose()
        if seen is None:
            gating_errors.append(
                "bloqueio do perdedor não observado em pg_stat_activity"
            )
        else:
            evidence.update(seen)
        return {
            "preference_id": "pref-race-pg",
            "checkout_url": f"{MP_URL_PREFIX}/race",
        }

    monkeypatch.setattr(pr, "create_preference", gated_preference)

    results = {}
    errors = []
    form = _product_form(user_email, product_id, key)

    winner_client = app.test_client()
    loser_client = app.test_client()
    _login(winner_client, user_email)
    _login(loser_client, user_email)

    def run_winner():
        try:
            resp = winner_client.post(
                "/pagamentos/mercado-pago", data=form,
            )
            results["winner"] = (
                resp.status_code,
                resp.headers.get("Location"),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(("winner", repr(exc)))

    def run_loser():
        try:
            if not winner_in_txn.wait(timeout=15):
                raise AssertionError(
                    "vencedor não entrou na transação"
                )
            resp = loser_client.post(
                "/pagamentos/mercado-pago", data=form,
            )
            results["loser"] = (
                resp.status_code,
                resp.headers.get("Location"),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(("loser", repr(exc)))

    winner_thread = threading.Thread(
        target=run_winner, name="winner",
    )
    loser_thread = threading.Thread(
        target=run_loser, name="loser",
    )

    captured, stop_capture = _capture_app_logs(app.logger)
    try:
        loser_thread.start()
        winner_thread.start()
        winner_thread.join(timeout=60)
        loser_thread.join(timeout=60)
    finally:
        stop_capture()

    assert not winner_thread.is_alive()
    assert not loser_thread.is_alive()
    assert errors == [], errors
    assert gating_errors == [], gating_errors

    # Evidência observável da corrida no banco.
    assert evidence.get("wait_event_type") == "Lock", evidence

    assert results["winner"] == (
        302, f"{MP_URL_PREFIX}/race",
    )
    assert results["loser"] == (
        302, f"{MP_URL_PREFIX}/race",
    )

    # Prova o mecanismo: o perdedor reaproveitou o pedido porque a
    # constraint UNIQUE recusou o INSERT dele.
    assert any(
        "constraint UNIQUE recusou o 2o INSERT" in message
        for message in captured
    ), captured

    assert len(calls) == 1
    with app.app_context():
        orders = (
            db.session.query(Order)
            .filter_by(user_id=user_id)
            .all()
        )
        assert len(orders) == 1
        assert orders[0].idempotency_key == key
        assert orders[0].id == calls[0]
        from loja.modules.models import Product

        product = db.session.get(Product, product_id)
        assert product.stock == 4
