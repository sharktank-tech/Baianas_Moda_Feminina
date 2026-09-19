"""Prova de unicidade de submissão em PostgreSQL (Fase 8.0.3) — HARD-OFF.

Só roda quando DATABASE_URL aponta para PostgreSQL. Usa schema descartável
`pollo_test_submit_*` e NUNCA toca `public` nem `pollomodas`:

- materializa um pedido ready no schema descartável (ORM, db.create_all);
- dispara DOIS threads tentando submeter a MESMA AliExpressOrder em sessões
  distintas ao mesmo tempo;
- o lock `SELECT ... FOR UPDATE` em dois estágios garante que exatamente UM
  thread realize a chamada ao provider (fake) => 1 submissão externa, 1 id;
- `SET SESSION lock_timeout` evita hang indefinido caso o lock falhe.

Este teste é a prova de que dois "workers" concorrentes não produzem dois
pedidos externos — proteção garantida no banco, não só em SQLite.
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
    """Provider fake thread-safe: conta chamadas, nunca faz rede."""

    _id_counter = 0
    _counter_lock = threading.Lock()

    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def submit(self, request):
        with self._lock:
            self.call_count += 1
        with CountingSubmitter._counter_lock:
            CountingSubmitter._id_counter += 1
            ext_id = str(99000 + CountingSubmitter._id_counter)
        return AliExpressSubmissionResult(
            external_order_ids=(ext_id,)
        )


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_%s_%s" % ("submit", secrets.token_hex(4))
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % schema))
        c.commit()

    # Credencial criptografada funcional.
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


def _materialize_ready(app):
    """Cria pedido AliExpress pago + fulfillment READY no schema descartável."""
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
    )

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG 8.0.3",
            email="u%d@pg.test" % uuid.uuid4().int,
            phone="11999999999",
            is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.flush()
        supplier = Supplier(
            id=uuid.uuid4(),
            name="Supp PG 8.0.3",
            channel_type="aliexpress",
            seller_id="SELLER_PG_803",
            default_currency="USD",
        )
        db.session.add(supplier)
        db.session.flush()
        product = Product(
            id=uuid.uuid4(),
            name="Prod PG 8.0.3",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_PG_803",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product)
        db.session.flush()
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id="100000803",
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
            supplier_sku_id="SKU_PG_803",
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
            provider_payment_id="mp_approved_%s" % uuid.uuid4().hex[:12],
            status="approved",
            amount=order.total,
            currency="BRL",
        ))
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order)
        ali_order = results[0].aliexpress_order
        materialize_snapshot(ali_order, session=db.session)
        db.session.commit()
        ali_order_id = results[0].aliexpress_order.id
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


def test_two_concurrent_submits_produce_one_external_order(env, monkeypatch):
    schema, app = env
    supplier_id, order_id, ali_order_id = _materialize_ready(app)

    submitter = CountingSubmitter()
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_submit,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_submit,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert "submitted" in results and len(results) == 2
    # Só UM thread venceu a corrida e executou o provider.
    assert submitter.call_count == 1

    from loja import db
    from loja.modules.models import AliExpressOrder

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "submitted"
        assert row.submitted_at is not None
        assert row.ali_order_id is not None
        assert row.supplier_id == supplier_id
        assert row.order_id == order_id
        # Uma única submissão externa.
        externais = AliExpressOrder.query.filter(
            AliExpressOrder.ali_order_id.isnot(None)
        ).all()
        assert len(externais) == 1
        assert externais[0].id == ali_order_id