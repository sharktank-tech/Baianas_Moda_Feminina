"""Prontidão em PostgreSQL: corretude com modelo real e concorrência.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
próprio e nunca toca public ou produção. Sem migração, compra, provider ou
mutação.
"""

import secrets
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

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

# Relógio congelado, igual aos testes SQLite.
FROZEN_NOW = datetime(2030, 6, 15, 10, 0)  # naive UTC

POLICY_VARS = (
    "UNIT_ECONOMICS_GATEWAY_FEE_PERCENT",
    "UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT",
    "UNIT_ECONOMICS_TAX_PERCENT",
    "UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER",
    "UNIT_ECONOMICS_REFUND_RESERVE_PERCENT",
)


@pytest.fixture(autouse=True)
def frozen_env(monkeypatch):
    for var in POLICY_VARS:
        monkeypatch.setenv(var, "0")


@pytest.fixture
def pg_app(monkeypatch):
    schema = "pollo_test_sales_ready_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "sales-ready-pg")
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

    yield app

    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _materialize(app):
    from loja import db
    from loja.modules.models import Product, Supplier, SupplierProduct, User

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Readiness",
            email="ready-%s@pg.test" % uuid.uuid4().hex,
            is_admin=False,
        )
        user.set_password("senha123")
        supplier = Supplier(
            id=uuid.uuid4(),
            name="Fornecedor PG",
            channel_type="aliexpress",
            seller_id="SELLER-PG-1",
        )
        good = Product(
            id=uuid.uuid4(),
            name="Produto PG Pronto",
            description="Descrição completa",
            price=Decimal("49.90"),
            image1="https://exemplo.com/img.png",
            stock=10,
            is_active=True,
            is_draft=False,
            source="aliexpress",
            currency="BRL",
            target_margin_percent=Decimal("30.00"),
            pricing_status="priced",
        )
        bad = Product(
            id=uuid.uuid4(),
            name="Produto PG Travado",
            description="Descrição completa",
            price=Decimal("49.90"),
            image1="https://exemplo.com/img.png",
            stock=0,
            is_active=True,
            is_draft=False,
            source="aliexpress",
            currency="BRL",
            target_margin_percent=Decimal("30.00"),
            pricing_status="priced",
        )
        db.session.add_all([user, supplier, good, bad])
        db.session.flush()
        link = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=good.id,
            supplier_product_id="SP-PG-1",
            cost=Decimal("20.00"),
            currency="BRL",
            freight_status="available",
            shipping_cost=Decimal("5.00"),
            landed_cost=Decimal("25.00"),
            freight_currency="BRL",
            is_active=True,
        )
        db.session.add(link)
        db.session.commit()
        return good.id, bad.id, link.id


def _fresh(link_id):
    return {
        str(link_id): {
            "cost": "5.00",
            "dest_prefix": "013",
            "option": None,
            "qty": 1,
            "currency": "BRL",
            "quoted_at": FROZEN_NOW - timedelta(minutes=10),
            "ttl_seconds": 1800,
        }
    }


def test_readiness_correctness_pg(pg_app):
    from loja import db
    from loja.modules.models import Product
    from loja.services import sales_readiness as sr

    good_id, bad_id, link_id = _materialize(pg_app)

    with pg_app.app_context():
        good = db.session.get(Product, good_id)
        bad = db.session.get(Product, bad_id)
        fresh = _fresh(link_id)
        ref = {"ref_dest_cep": "01310100", "ref_option": None}
        good_result = sr.evaluate_product_readiness(
            good,
            fresh_quotes=fresh,
            now=FROZEN_NOW,
            charged_freight=Decimal("10.00"),
            **ref,
        )
        bad_result = sr.evaluate_product_readiness(
            bad,
            fresh_quotes=fresh,
            now=FROZEN_NOW,
            charged_freight=Decimal("10.00"),
            **ref,
        )

    assert good_result.state == sr.READY
    assert bad_result.state == sr.BLOCKED
    assert "no_stock" in bad_result.reasons


def test_concurrent_evaluate_no_mutation_pg(pg_app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services import sales_readiness as sr

    good_id, _, link_id = _materialize(pg_app)
    barrier = threading.Barrier(6)
    results = []
    errors = []

    def worker():
        try:
            barrier.wait(10)
            with pg_app.app_context():
                product = db.session.get(Product, good_id)
                results.append(
                    sr.evaluate_product_readiness(
                        product,
                        fresh_quotes=_fresh(link_id),
                        now=FROZEN_NOW,
                        charged_freight=Decimal("10.00"),
                        ref_dest_cep="01310100",
                        ref_option=None,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - prova que nada travou
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert len(results) == 6
    assert {r.state for r in results} == {sr.READY}
    assert len({r.margin_display for r in results}) == 1

    with pg_app.app_context():
        assert Product.query.count() == 2
        assert Order.query.count() == 0
        assert db.session.get(Product, good_id).stock == 10
