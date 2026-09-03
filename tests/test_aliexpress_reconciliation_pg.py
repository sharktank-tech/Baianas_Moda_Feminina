"""Prova de reconciliação concorrente em PostgreSQL (Fase 8.0.5) — HARD-OFF.

Só roda quando DATABASE_URL aponta para PostgreSQL. Usa schema descartável
`pollo_test_reconcile_*` e NUNCA toca `public` nem `pollomodas`:

- materializa um pedido READY e o leva a ``pending_reconciliation``
  (submission ambíguo: timeout sem confirmação);
- Cenário A: submit × reconcile concorrentes na MESMA AliExpressOrder:
  nenhuma nova criação externa acontece (submitter nunca é chamado), a
  reconciliação acontece exatamente 1x e o fim é ``submitted`` consistente;
- Cenário B: DOIS reconciles concorrentes na MESMA AliExpressOrder:
  o lock ``SELECT ... FOR UPDATE`` serializa e exatamente UM vence
  (reconciler chamado 1x); o perdedor relê e no-op; fim ``submitted``.

Prova de que a máquina 8.0.5 não cria duas submissões nem reconciliações
duplicadas sob concorrência real de "workers".
"""
import secrets
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL
from loja.integrations.aliexpress.reconciliation import (
    AliExpressOrderReconciler,
    AliExpressOrderReconciliationResult,
    reconcile_aliexpress_order,
    RECONCILE_FOUND,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
    AliExpressSubmissionUnknown,
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
    """Provider fake thread-safe: conta chamadas, nunca faz rede."""

    def __init__(self, *, outcome_override=None):
        self.call_count = 0
        self._lock = threading.Lock()
        self.outcome_override = outcome_override

    def submit(self, request):
        with self._lock:
            self.call_count += 1
        if self.outcome_override is AliExpressSubmissionUnknown:
            raise AliExpressSubmissionUnknown("timeout sem confirmação")
        return AliExpressSubmissionResult(external_order_id=f"AE-PG-{secrets.token_hex(4)}")


class CountingReconciler(AliExpressOrderReconciler):
    """Reconciler fake thread-safe: sempre FOUND, conta chamadas."""

    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def reconcile(self, request):
        with self._lock:
            self.call_count += 1
        return AliExpressOrderReconciliationResult(
            outcome=RECONCILE_FOUND, external_order_id="REMOTE-PG-805"
        )


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_%s_%s" % ("reconcile", secrets.token_hex(4))
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


def _materialize_pending(app):
    """Pedido READY + submission ambígua => AliExpressOrder pending_reconciliation."""
    from loja import db
    from loja.integrations.aliexpress.credential_store import persist_credential
    from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import (
        Product,
        ProductVariant,
        Supplier,
        SupplierProduct,
        User,
    )

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG 8.0.5",
            email="u%d@pg.test" % uuid.uuid4().int,
            phone="11999999999",
            is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.flush()
        supplier = Supplier(
            id=uuid.uuid4(),
            name="Supp PG 8.0.5",
            channel_type="aliexpress",
            seller_id="SELLER_PG_805",
            default_currency="USD",
        )
        db.session.add(supplier)
        db.session.flush()
        product = Product(
            id=uuid.uuid4(),
            name="Prod PG 8.0.5",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_PG_805",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product)
        db.session.flush()
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id="100000805",
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
            supplier_sku_id="SKU_PG_805",
            sku=None,
            color="Red",
            size="M",
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
            status="paid",
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
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order)
        assert len(results) == 1
        assert results[0].state == "ready"
        db.session.commit()
        ali_order_id = results[0].aliexpress_order.id

        from loja.modules.models import AliExpressOrder

        ali_order = db.session.get(AliExpressOrder, ali_order_id)
        amb = submit_aliexpress_order(
            ali_order,
            submitter=CountingSubmitter(outcome_override=AliExpressSubmissionUnknown),
        )
        assert amb.state == "pending_reconciliation"
        db.session.commit()
        return supplier.id, order.id, ali_order_id


def _start_submit(app, ali_order_id, submitter, results, errors, barrier):
    from loja import db

    try:
        with app.app_context():
            db.session.connection().exec_driver_sql(
                "SET SESSION lock_timeout = 15000"
            )
            barrier.wait(timeout=30)
            from loja.modules.models import AliExpressOrder

            ali_order = db.session.get(AliExpressOrder, ali_order_id)
            res = submit_aliexpress_order(ali_order, submitter=submitter)
            results.append(res.state)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def _start_reconcile(app, ali_order_id, reconciler, results, errors, barrier):
    from loja import db

    try:
        with app.app_context():
            db.session.connection().exec_driver_sql(
                "SET SESSION lock_timeout = 15000"
            )
            barrier.wait(timeout=30)
            from loja.modules.models import AliExpressOrder

            ali_order = db.session.get(AliExpressOrder, ali_order_id)
            res = reconcile_aliexpress_order(ali_order, reconciler=reconciler)
            results.append(res.state)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_submit_vs_reconcile_never_creates_second_submission(env):
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_pending(app)

    submitter = CountingSubmitter(outcome_override=AliExpressSubmissionUnknown)
    reconciler = CountingReconciler()
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_submit,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_reconcile,
        args=(app, ali_order_id, reconciler, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert set(results) == {"pending_reconciliation", "submitted"}
    # Nenhuma criação externa nova (submitter nunca chamado); só 1 reconciliação.
    assert submitter.call_count == 0
    assert reconciler.call_count == 1

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "submitted"
        assert row.ali_order_id == "REMOTE-PG-805"
        assert row.submitted_at is not None
        assert row.attempt_key is not None
        assert row.supplier_id == supplier_id
        assert row.order_id == order_id


def test_two_concurrent_reconciles_apply_once(env):
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_pending(app)

    reconciler = CountingReconciler()
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_reconcile,
        args=(app, ali_order_id, reconciler, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_reconcile,
        args=(app, ali_order_id, reconciler, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert results == ["submitted", "submitted"]
    # Exatamente uma reconciliação externa foi aplicada.
    assert reconciler.call_count == 1

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "submitted"
        assert row.ali_order_id == "REMOTE-PG-805"
        assert row.submitted_at is not None
        # Consistency check com o mesmo estado observado pelos dois threads.
        externais = AliExpressOrder.query.filter(
            AliExpressOrder.ali_order_id.isnot(None)
        ).all()
        assert len(externais) == 1
        assert externais[0].id == ali_order_id