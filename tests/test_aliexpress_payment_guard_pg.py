"""Prova de corrida pagamento x fulfillment em PostgreSQL (Fase 8.1.0) — HARD-OFF.

Só roda quando DATABASE_URL aponta para PostgreSQL (dialeto ``postgresql``).
Usa schema descartável ``pollo_test_payment_guard_*`` criado e dropado por
teste; NUNCA toca ``public``, a base de testes compartilhada nem banco de
produção.

Em sessões distintas (READ COMMITTED padrão do PostgreSQL), com a ordem das
transações controlada por eventos/barreira:

- Cenário A: antes do COMMIT da aprovação do pagamento, o fulfillment é
  bloqueado SEM criar AliExpressOrder, AliExpressOrderItem, tentativa
  (ledger) ou identidade remota, e SEM chamar refresher/submitter.
- Cenário B: depois do COMMIT, o pagamento aprovado e vinculado ao pedido
  permite o fulfillment (exatamente 1 AliExpressOrder ready).
- Cenário C: duas chamadas concorrentes após a aprovação NÃO duplicam a
  AliExpressOrder (unicidade por idempotency_key + SELECT ... FOR UPDATE).

Este arquivo usa somente dados sintéticos e providers fake; nenhuma chamada
HTTP é feita a AliExpress.
"""
import secrets
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from conftest import ORIGINAL_DATABASE_URL

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    IS_POSTGRES = (
        bool(DATABASE_URL)
        and make_url(DATABASE_URL).get_backend_name() == "postgresql"
    )
except Exception:
    IS_POSTGRES = False

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL (DATABASE_URL postgresql://)",
)


class CountingRefresher:
    """Refresher OAuth fake thread-safe: conta chamadas, nunca faz rede."""

    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.call_count += 1
        return None


class CountingSubmitter:
    """Submitter fake thread-safe: conta chamadas, nunca faz rede."""

    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def submit(self, request):
        with self._lock:
            self.call_count += 1
        return None


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_%s_%s" % ("payment_guard", secrets.token_hex(4))
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % schema))
        c.commit()

    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "test-secret")
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

    from loja import create_app

    app = create_app()
    with app.app_context():
        from loja import db

        db.create_all()

    yield schema, app

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % schema))
        c.commit()


def _materialize_order(app, *, committed_status="pending", payment_status="pending"):
    """Pedido com 1 OrderItem válido (AliExpress) + Payment na base committed.

    payment_status/status são gravados no banco ANTES do teste para que a
    determinação do estado vigente seja observável por outras sessões.
    """
    from loja.modules.getway_mercado_pago.payment_models import (
        Order,
        OrderItem,
        Payment,
    )
    from loja.modules.models import (
        Product,
        ProductVariant,
        Supplier,
        SupplierProduct,
        User,
    )

    with app.app_context():
        from loja import db
        from loja.integrations.aliexpress.credential_store import persist_credential

        user = User(
            id=uuid.uuid4(),
            name="PG guard 8.1.0",
            email="u%d@pg.test" % uuid.uuid4().int,
            phone="11999999999",
            is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.flush()
        supplier = Supplier(
            id=uuid.uuid4(),
            name="Supp PG guard",
            channel_type="aliexpress",
            seller_id="SELLER_PG_810",
            default_currency="USD",
        )
        db.session.add(supplier)
        db.session.flush()
        product = Product(
            id=uuid.uuid4(),
            name="Prod PG guard",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_PG_810",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product)
        db.session.flush()
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id="100000810",
            cost=Decimal("10.00"),
            currency="USD",
            is_active=True,
        )
        db.session.add(sp)
        db.session.flush()
        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=sp.id,
            supplier_sku_id="SKU_PG_810",
            sku=None,
            color="Red",
            size="M",
            attributes=[
                {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
                {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
            ],
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.flush()
        persist_credential(
            supplier_id=supplier.id,
            seller_id=supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.flush()
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status=committed_status,
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="U",
            customer_email="u@pg.test",
            customer_phone="11999999999",
            zip_code="01001000",
            street="Rua",
            number="1",
            complement="",
            neighborhood="C",
            city="SP",
            state="SP",
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=product.id,
            variant_id=variant.id,
            product_name=product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        ))
        db.session.add(Payment(
            id=uuid.uuid4(),
            order_id=order.id,
            provider_payment_id="mp_%s" % uuid.uuid4().hex[:12],
            status=payment_status,
            amount=order.total,
            currency="BRL",
        ))
        db.session.commit()
        return order.id


def _count_side_effects(app):
    """Tentativas (ledger) e identidades remotas criadas no schema (base vazia)."""
    with app.app_context():
        from loja import db
        from loja.modules.models import (
            AliExpressOrder,
            AliExpressOrderItem,
            AliExpressRemoteOrder,
            AliExpressSubmissionAttempt,
        )

        return {
            "orders": AliExpressOrder.query.count(),
            "items": AliExpressOrderItem.query.count(),
            "attempts": AliExpressSubmissionAttempt.query.count(),
            "remote": AliExpressRemoteOrder.query.count(),
        }


def test_before_commit_approval_blocks_without_side_effects(env):
    """Pedido pending + Payment pending; aprovação escrita mas NÃO commitada.

    O fulfillment em outra sessão não vê a aprovação: bloqueado sem criar
    nada e sem chamar refresher/submitter.
    """
    schema, app = env
    order_id = _materialize_order(app)  # committed: status=pending, payment=pending
    refresher = CountingRefresher()
    submitter = CountingSubmitter()
    prepared = threading.Event()
    released = threading.Event()
    results = []
    errors = []

    def confirm():
        try:
            with app.app_context():
                from loja import db
                from loja.modules.getway_mercado_pago.payment_models import (
                    Order,
                    Payment,
                )

                order = db.session.get(Order, order_id)
                order.status = "paid"
                payment = Payment.query.filter_by(order_id=order_id).first()
                payment.status = "approved"
                payment.amount = order.total
                payment.currency = "BRL"
                db.session.flush()  # gravado no servidor, sem COMMIT
                prepared.set()
                assert released.wait(30)
                db.session.commit()  # aprovação só é commitada depois
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            prepared.set()
            released.set()

    def fulfill():
        try:
            with app.app_context():
                from loja import db
                from loja.integrations.aliexpress.fulfillment import (
                    ensure_aliexpress_fulfillment,
                )
                from loja.modules.getway_mercado_pago.payment_models import Order

                db.session.connection().exec_driver_sql(
                    "SET SESSION lock_timeout = 15000"
                )
                assert prepared.wait(30)
                order = db.session.get(Order, order_id)
                results.append(
                    ensure_aliexpress_fulfillment(
                        order, refresher=refresher
                    )
                )
                released.set()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            released.set()

    t1 = threading.Thread(target=confirm)
    t2 = threading.Thread(target=fulfill)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert results == [[]], results  # bloqueado antes do commit da aprovação
    counts = _count_side_effects(app)
    assert counts == {
        "orders": 0,
        "items": 0,
        "attempts": 0,
        "remote": 0,
    }, counts
    assert refresher.call_count == 0
    assert submitter.call_count == 0


def test_before_commit_approval_blocks_paid_order(env):
    """Pedido já ``paid`` committed, mas aprovação do Payment NÃO commitada.

    O guard ainda bloqueia (evento financeiro vigente não aprovado) sem efeito
    colateral e sem chamar serviços externos.
    """
    schema, app = env
    order_id = _materialize_order(
        app, committed_status="paid", payment_status="pending"
    )
    refresher = CountingRefresher()
    submitter = CountingSubmitter()
    prepared = threading.Event()
    released = threading.Event()
    results = []
    errors = []

    def confirm():
        try:
            with app.app_context():
                from loja import db
                from loja.modules.getway_mercado_pago.payment_models import Payment

                payment = Payment.query.filter_by(order_id=order_id).first()
                payment.status = "approved"
                payment.amount = Decimal("29.90")
                payment.currency = "BRL"
                db.session.flush()
                prepared.set()
                assert released.wait(30)
                db.session.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            prepared.set()
            released.set()

    def fulfill():
        try:
            with app.app_context():
                from loja import db
                from loja.integrations.aliexpress.fulfillment import (
                    ensure_aliexpress_fulfillment,
                )
                from loja.modules.getway_mercado_pago.payment_models import Order

                db.session.connection().exec_driver_sql(
                    "SET SESSION lock_timeout = 15000"
                )
                assert prepared.wait(30)
                order = db.session.get(Order, order_id)
                results.append(
                    ensure_aliexpress_fulfillment(order, refresher=refresher)
                )
                released.set()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            released.set()

    t1 = threading.Thread(target=confirm)
    t2 = threading.Thread(target=fulfill)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert results == [[]], results  # pedido paid, sem payment approved commitado
    counts = _count_side_effects(app)
    assert counts == {
        "orders": 0,
        "items": 0,
        "attempts": 0,
        "remote": 0,
    }, counts
    assert refresher.call_count == 0
    assert submitter.call_count == 0


def test_after_commit_approval_allows_fulfillment(env):
    """Depois do COMMIT, pagamento aprovado vinculado permite o fulfillment."""
    schema, app = env
    order_id = _materialize_order(app)  # pending/pending
    refresher = CountingRefresher()

    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order, Payment

        order = db.session.get(Order, order_id)
        order.status = "paid"
        payment = Payment.query.filter_by(order_id=order_id).first()
        payment.status = "approved"
        payment.amount = order.total
        payment.currency = "BRL"
        db.session.commit()  # aprovação commitada

    with app.app_context():
        from loja import db
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        from loja.modules.getway_mercado_pago.payment_models import Order
        from loja.modules.models import AliExpressOrder, AliExpressOrderItem

        order = db.session.get(Order, order_id)
        results = ensure_aliexpress_fulfillment(order, refresher=refresher)
        assert len(results) == 1
        assert results[0].state == "ready"
        ao_id = results[0].aliexpress_order.id
        db.session.commit()
        assert AliExpressOrder.query.filter_by(order_id=order_id).count() == 1
        assert (
            AliExpressOrderItem.query.filter_by(
                aliexpress_order_id=ao_id
            ).count()
            == 1
        )

    # Credencial válida: refresh não é chamado no caminho permitido.
    assert refresher.call_count == 0


def test_two_concurrent_after_approval_do_not_duplicate(env):
    """Duas chamadas concorrentes pós-aprovação => exatamente 1 AliExpressOrder.

    A unicidade é garantida por idempotency_key único + FOR UPDATE: o perdedor
    da corrida reutiliza/compartilha o INSERT e, no pior caso, recebe
    IntegrityError de unicidade — nunca criação duplicada.
    """
    schema, app = env
    order_id = _materialize_order(
        app, committed_status="paid", payment_status="approved"
    )
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def run():
        try:
            with app.app_context():
                from loja import db
                from loja.integrations.aliexpress.fulfillment import (
                    ensure_aliexpress_fulfillment,
                )
                from loja.modules.getway_mercado_pago.payment_models import Order

                db.session.connection().exec_driver_sql(
                    "SET SESSION lock_timeout = 15000"
                )
                barrier.wait(timeout=30)
                order = db.session.get(Order, order_id)
                res = ensure_aliexpress_fulfillment(order)
                db.session.commit()
                results.append([r.state for r in res])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert len(results) + len(errors) == 2
    for exc in errors:
        # Único erro aceitável: corrida de unicidade do idempotency_key.
        assert isinstance(exc, IntegrityError), errors

    with app.app_context():
        from loja import db
        from loja.modules.models import (
            AliExpressOrder,
            AliExpressOrderItem,
        )

        assert AliExpressOrder.query.filter_by(order_id=order_id).count() == 1
        items = (
            AliExpressOrderItem.query.join(AliExpressOrder)
            .filter(AliExpressOrder.order_id == order_id)
            .count()
        )
        assert items == 1

    assert any("ready" in r for r in results), (results, errors)