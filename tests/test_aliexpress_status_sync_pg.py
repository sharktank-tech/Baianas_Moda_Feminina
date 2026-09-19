"""Prova de sincronização de status concorrente em PostgreSQL (Fase 8.0.6).

Só roda quando DATABASE_URL aponta para PostgreSQL. Usa schema descartável
`pollo_test_status_*` e NUNCA toca `public` nem `pollomodas`:

- materializa AliExpressOrder SUBMITTED (submission ok) no schema descartável;
- Cenário A: DOIS threads sincronizam a MESMA ordem para o MESMO estágio
  (delivered): o lock ``SELECT ... FOR UPDATE`` serializa; resultado final
  determinístico; nenhuma regressão; timestamps coerentes (uma aplicação);
- Cenário B: dois resultados concorrentes com estágios DIFERENTES (confirmed
  vs delivered): o estado final é o estágio monotonicamente mais avançado
  permitido (delivered), sem duplicação nem divergência.

Prova de que a máquina monótona 8.0.6 não regride nem duplica sob corrida
real de "workers" (SQLite não é prova de locking).
"""
import secrets
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL
from tests.aliexpress_snapshot_utils import materialize_snapshot
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusSyncer,
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusResult,
    sync_aliexpress_order_status,
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_SHIPPED,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
    submit_aliexpress_order,
)

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


class CountingSubmitter(AliExpressOrderSubmitter):
    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def submit(self, request):
        with self._lock:
            self.call_count += 1
        return AliExpressSubmissionResult(external_order_ids=(f"991100{self.call_count:03d}",))


class FixedSyncer(AliExpressOrderStatusSyncer):
    """Retorna sempre o mesmo outcome; conta chamadas thread-safe."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.call_count = 0
        self._lock = threading.Lock()

    def sync(self, request: AliExpressOrderStatusRequest):
        with self._lock:
            self.call_count += 1
        return AliExpressOrderStatusResult(outcome=self.outcome)


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_%s_%s" % ("status", secrets.token_hex(4))
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


def _materialize_submitted(app):
    from loja import db
    from loja.integrations.aliexpress.credential_store import persist_credential
    from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
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
        AliExpressOrder,
    )

    with app.app_context():
        user = User(
            id=uuid.uuid4(), name="PG 8.0.6", email="u%d@pg.test" % uuid.uuid4().int,
            phone="11999999999", is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.flush()
        supplier = Supplier(
            id=uuid.uuid4(), name="Supp PG 8.0.6", channel_type="aliexpress",
            seller_id="SELLER_PG_806", default_currency="USD",
        )
        db.session.add(supplier)
        product = Product(
            id=uuid.uuid4(), name="Prod PG 8.0.6", description="Test",
            price=Decimal("29.90"), source="aliexpress",
            external_id="EXT_PG_806", is_draft=True, is_active=False,
        )
        db.session.add(product)
        db.session.flush()
        sp = SupplierProduct(
            id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id,
            supplier_product_id="100000806", cost=Decimal("10.00"),
            currency="USD", is_active=True,
        )
        db.session.add(sp)
        db.session.flush()
        variant = ProductVariant(
            id=uuid.uuid4(), product_id=product.id, supplier_product_id=sp.id,
            supplier_sku_id="SKU_PG_806", sku=None, color="Red", size="M",
            attributes=[
                {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
                {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
            ],
            stock=10, price=Decimal("29.90"), is_active=True,
        )
        db.session.add(variant)
        db.session.flush()
        persist_credential(
            supplier_id=supplier.id, seller_id=supplier.seller_id,
            access_token="tok", expires_in=3600,
        )
        db.session.flush()
        order = Order(
            id=uuid.uuid4(), user_id=user.id, checkout_type="product",
            status="paid", subtotal=Decimal("29.90"), total=Decimal("29.90"),
            stock_reserved=False, customer_name="U", customer_email="u@pg.test",
            customer_phone="11999999999", zip_code="01001000", street="Rua",
            number="1", complement="", neighborhood="C", city="SP", state="SP",
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(
            id=uuid.uuid4(), order_id=order.id, product_id=product.id,
            variant_id=variant.id, product_name=product.name, quantity=1,
            unit_price=Decimal("29.90"), line_total=Decimal("29.90"),
        ))
        db.session.add(Payment(
            id=uuid.uuid4(), order_id=order.id,
            provider_payment_id="mp_approved_%s" % uuid.uuid4().hex[:12],
            status="approved", amount=order.total, currency="BRL",
        ))
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order)
        assert results[0].state == "ready"
        db.session.commit()
        ali_order_id = results[0].aliexpress_order.id

        ali_order = db.session.get(AliExpressOrder, ali_order_id)
        materialize_snapshot(ali_order, session=db.session)
        res = submit_aliexpress_order(ali_order, submitter=CountingSubmitter())
        assert res.state == "submitted"
        db.session.commit()
        return supplier.id, order.id, ali_order_id


def _start_sync(app, ali_order_id, syncer, results, errors, barrier):
    from loja import db

    try:
        with app.app_context():
            db.session.connection().exec_driver_sql(
                "SET SESSION lock_timeout = 15000"
            )
            barrier.wait(timeout=30)
            from loja.modules.models import AliExpressOrder

            ali_order = db.session.get(AliExpressOrder, ali_order_id)
            res = sync_aliexpress_order_status(ali_order, syncer=syncer)
            results.append(res.state)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_two_concurrent_syncs_same_stage_deterministic(env):
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_submitted(app)

    syncer = FixedSyncer(STATUS_DELIVERED)
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert results == ["delivered", "delivered"]
    # Lock serializa: exatamente UMA aplicação acontece.
    assert syncer.call_count == 2  # ambos consultam, mas só um aplica transição
    assert len({r for r in results}) <= 1

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "delivered"
        assert row.confirmed_at is not None
        assert row.shipped_at is not None
        assert row.delivered_at is not None
        assert row.ali_status == "delivered"
        assert row.supplier_id == supplier_id
        assert row.order_id == order_id
        assert row.last_error is None


def test_two_concurrent_syncs_distinct_stages_pick_most_advanced(env):
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_submitted(app)

    syncer_a = FixedSyncer(STATUS_CONFIRMED)
    syncer_b = FixedSyncer(STATUS_DELIVERED)
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer_a, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer_b, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    # Monotônica: se delivered vence a corrida, o thread confirmed lê delivered e
    # retorna delivered (não regride). Aceita ambos delivered como válido.
    assert len(results) == 2
    assert set(results) <= {"confirmed", "delivered"}
    assert "delivered" in results

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "delivered"
        assert row.delivered_at is not None
        assert row.ali_status == "delivered"


def test_concurrent_confirmed_vs_shipped_milestone_staging(env):
    """confirmed vs shipped concorrentes: fim shipped, SEM delivered_at."""
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_submitted(app)

    syncer_a = FixedSyncer(STATUS_CONFIRMED)
    syncer_b = FixedSyncer(STATUS_SHIPPED)
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer_a, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer_b, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    # Se shipped vence primeiro, confirmed retorna shipped (monotônica).
    assert len(results) == 2
    assert set(results) <= {"confirmed", "shipped"}
    assert "shipped" in results

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "shipped"
        assert row.confirmed_at is not None
        assert row.shipped_at is not None
        assert row.delivered_at is None  # milestone futuro não é criado


def test_concurrent_shipped_vs_delivered_milestone_staging(env):
    """shipped vs delivered concorrentes: fim delivered, todos os milestones."""
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_submitted(app)

    syncer_a = FixedSyncer(STATUS_SHIPPED)
    syncer_b = FixedSyncer(STATUS_DELIVERED)
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer_a, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_sync,
        args=(app, ali_order_id, syncer_b, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    # Se delivered vence a corrida, o thread shipped lê delivered e retorna delivered.
    assert len(results) == 2
    assert set(results) <= {"shipped", "delivered"}
    assert "delivered" in results

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "delivered"
        assert row.confirmed_at is not None
        assert row.shipped_at is not None
        assert row.delivered_at is not None