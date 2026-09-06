"""PostgreSQL concurrency proof for the Fase 8.0.11H controlled canary.

Runs only when DATABASE_URL is PostgreSQL and uses a disposable schema.
No network I/O is performed.

Proof:
* the canary is armed for exactly one AliExpressOrder;
* two threads enter the controlled boundary concurrently for that same order;
* the PostgreSQL session advisory lock serializes both wrappers across the
  internal durable-attempt commits;
* the core row lock/idempotency then guarantees exactly one provider call and
  one submitted remote identity.
"""

import secrets
import threading
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL
from tests.aliexpress_snapshot_utils import materialize_snapshot
from loja.integrations.aliexpress.controlled_canary import submit_controlled_canary_order
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
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


class SlowCountingSubmitter(AliExpressOrderSubmitter):
    """Fake provider widened enough to expose a wrapper-level race."""

    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def submit(self, request):
        with self._lock:
            self.call_count += 1
            call_no = self.call_count
        time.sleep(0.25)
        return AliExpressSubmissionResult(
            external_order_ids=(str(991000 + call_no),)
        )


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_canary_%s" % secrets.token_hex(4)
    eng = create_engine(DATABASE_URL)
    with eng.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % schema))
        connection.commit()

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

    yield schema, app, monkeypatch

    with eng.connect() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % schema))
        connection.commit()


def _materialize_ready(app):
    from loja import db
    from loja.integrations.aliexpress.credential_store import persist_credential
    from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct, User

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Canary",
            email="canary-%s@pg.test" % uuid.uuid4().hex,
            phone="11999999999",
            is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.flush()

        supplier = Supplier(
            id=uuid.uuid4(),
            name="Supp PG Canary",
            channel_type="aliexpress",
            seller_id="SELLER_PG_CANARY",
            default_currency="USD",
        )
        db.session.add(supplier)
        db.session.flush()

        product = Product(
            id=uuid.uuid4(),
            name="Prod PG Canary",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_PG_CANARY",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product)
        db.session.flush()

        supplier_product = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id="100000811",
            cost=Decimal("10.00"),
            currency="USD",
            is_active=True,
        )
        db.session.add(supplier_product)
        db.session.flush()

        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            supplier_sku_id="SKU_PG_CANARY",
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
            status="paid",
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="U",
            customer_email=user.email,
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
        db.session.add(
            OrderItem(
                id=uuid.uuid4(),
                order_id=order.id,
                product_id=product.id,
                variant_id=variant.id,
                product_name=product.name,
                quantity=1,
                unit_price=Decimal("29.90"),
                line_total=Decimal("29.90"),
            )
        )
        db.session.commit()

        result = ensure_aliexpress_fulfillment(order)[0]
        ali_order = result.aliexpress_order
        materialize_snapshot(ali_order, session=db.session)
        db.session.commit()
        return ali_order.id


def _start_canary(app, ali_order_id, submitter, results, errors, barrier):
    from loja import db
    from loja.modules.models import AliExpressOrder

    try:
        with app.app_context():
            db.session.connection().exec_driver_sql(
                "SET SESSION lock_timeout = 15000"
            )
            barrier.wait(timeout=30)
            ali_order = db.session.get(AliExpressOrder, ali_order_id)
            result = submit_controlled_canary_order(
                ali_order,
                submitter=submitter,
                session=db.session,
            )
            results.append(result.state)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_two_concurrent_controlled_canaries_call_provider_once(env):
    schema, app, monkeypatch = env
    ali_order_id = _materialize_ready(app)

    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", str(ali_order_id))

    submitter = SlowCountingSubmitter()
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_canary,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_canary,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert errors == [], errors
    assert results == ["submitted", "submitted"] or results == ["submitted", "submitted"]
    assert submitter.call_count == 1

    from loja import db
    from loja.modules.models import AliExpressOrder, AliExpressRemoteOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "submitted"
        assert row.submitted_at is not None
        remotes = AliExpressRemoteOrder.query.filter_by(
            aliexpress_order_id=ali_order_id
        ).all()
        assert len(remotes) == 1
