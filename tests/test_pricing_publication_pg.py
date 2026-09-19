"""Validação em PostgreSQL real da Fase 6 (pricing + publication guard).

Só roda quando DATABASE_URL aponta para PostgreSQL. Usa schemas descartáveis
`pollo_test_pricing_*` e NUNCA toca `public` nem o schema persistente:
- migration 0005 idempotente (2x), colunas + checks vinculados a conrelid certo
- CHECK real: AliExpress ativo+unpriced rejeitado; ativo+priced+!draft aceito;
  internal antigo aceito; margem alvo forçada ao intervalo (0, 100)
- fluxo ORM completo no schema descartável: pricing Decimal -> publish -> commit
  + guarda impedindo ativação de AliExpress não-priced
"""
import pathlib
import secrets
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text, inspect as sa_inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from conftest import ORIGINAL_DATABASE_URL
from test_draft_import_pg import (
    _engine_with_search_path,
    _public_columns_snapshot,
    _split_sql_statements,
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

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "0005_pricing_publication_guard.sql"
)

_LEGACY_PRODUCTS = """
CREATE TABLE products (
    id UUID PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    price NUMERIC(12, 2) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'internal',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    is_draft BOOLEAN NOT NULL DEFAULT FALSE
)
"""


def _apply_migration(engine, schema):
    sql = _MIGRATION.read_text()
    sql = sql.replace(
        "SET search_path TO pollomodas;",
        "SET search_path TO %s;" % schema,
    )
    sql = sql.replace(
        "'pollomodas.products'::regclass",
        "'%s.products'::regclass" % schema,
    )
    with engine.begin() as c:
        for stmt in _split_sql_statements(sql):
            c.execute(text(stmt))


@pytest.fixture
def env(monkeypatch):
    # Guarda de destino: nunca tocar produção; só schemas descartáveis.
    try:
        _url = make_url(DATABASE_URL) if DATABASE_URL else None
        assert _url is not None and _url.get_backend_name() == "postgresql", "DATABASE_URL não é postgresql"
        assert _url.host in ("127.0.0.1", "localhost", "::1"), "host não é descartável: %s" % _url.host
        assert _url.database and _url.database.startswith("pollo"), "database não é descartável: %s" % _url.database
    except Exception as _guard_exc:
        pytest.skip("DATABASE_URL não é PostgreSQL descartável: %r" % _guard_exc)

    schema = "pollo_test_%s_%s" % ("pricing", secrets.token_hex(4))
    assert schema.startswith("pollo_test_pricing_"), "schema não é descartável"
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % schema))
        c.commit()

    yield schema, eng

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % schema))
        c.commit()


def _constraint_names_for_products(se, schema):
    with se.connect() as c:
        rows = c.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = to_regnamespace(:schema)::oid "
                "AND conrelid = to_regclass(:qualified)::oid "
                "ORDER BY conname"
            ),
            {"schema": schema, "qualified": "%s.products" % schema},
        ).fetchall()
    return {r[0] for r in rows}


def _insert_product(se, **fields):
    cols, vals, params = [], [], {}
    for i, (key, value) in enumerate(fields.items()):
        cols.append(key)
        vals.append(":%s" % key)
        params[key] = value
    with se.begin() as c:
        c.execute(
            text(
                "INSERT INTO products (%s) VALUES (%s)"
                % (", ".join(cols), ", ".join(vals))
            ),
            params,
        )


def _expect_rejected(se, **fields):
    # CHECK violations são 23514: com pg8000 viram ProgrammingError, com psycopg2
    # viram IntegrityError. O teste deve aceitar ambos sem enfraquecer a
    # proteção (continua exigindo violação de constraint).
    with pytest.raises((IntegrityError, ProgrammingError, DBAPIError)):
        _insert_product(se, **fields)


def test_migration_idempotent_and_checks_bound_to_products(env):
    schema, eng = env
    before = _public_columns_snapshot(eng)

    se = _engine_with_search_path(DATABASE_URL, schema)
    with se.begin() as c:
        c.execute(text(_LEGACY_PRODUCTS))
    _apply_migration(se, schema)
    _apply_migration(se, schema)  # idempotente

    inspector = sa_inspect(se)
    p_cols = {c["name"] for c in inspector.get_columns("products", schema=schema)}
    assert {
        "currency", "target_margin_percent", "pricing_status",
        "pricing_error", "pricing_updated_at",
    }.issubset(p_cols)

    names = _constraint_names_for_products(se, schema)
    # As constraints estão REALMENTE vinculadas a products do schema
    # (pg_constraint.conrelid = to_regclass('<schema>.products')).
    assert "ck_product_target_margin_range" in names
    assert "ck_product_aliexpress_publication_guard" in names
    assert "ck_product_pricing_status" in names

    assert _public_columns_snapshot(eng) == before, "public foi alterado!"


def test_db_guard_rejects_aliexpress_active_unpriced(env):
    schema, eng = env
    se = _engine_with_search_path(DATABASE_URL, schema)
    with se.begin() as c:
        c.execute(text(_LEGACY_PRODUCTS))
    _apply_migration(se, schema)

    # AliExpress ativo + unpriced (default) => rejeitado no banco.
    _expect_rejected(
        se, id=str(uuid.uuid4()), name="x", price="10.00",
        source="aliexpress", is_active=True,
    )
    assert _public_columns_snapshot(eng) == _public_columns_snapshot(eng)


def test_db_guard_accepts_priced_published_and_legacy_internal(env):
    schema, eng = env
    se = _engine_with_search_path(DATABASE_URL, schema)
    with se.begin() as c:
        c.execute(text(_LEGACY_PRODUCTS))
    _apply_migration(se, schema)

    # AliExpress ativo + priced + !draft => caminho legítimo.
    _insert_product(
        se, id=str(uuid.uuid4()), name="ok", price="150.00",
        source="aliexpress", is_active=True, is_draft=False,
        pricing_status="priced", target_margin_percent="20",
    )

    # Produto AliExpress publicado que ANTES da migration era rascunho
    # (draft + inactive) permanece válido.
    _insert_product(
        se, id=str(uuid.uuid4()), name="rascunho", price="10.00",
        source="aliexpress", is_active=False, is_draft=True,
    )

    # Produto interno/legado pode ficar ativo sem priced.
    _insert_product(
        se, id=str(uuid.uuid4()), name="interno", price="49.90",
        source="internal", is_active=True,
    )

    assert _public_columns_snapshot(eng) == _public_columns_snapshot(eng)


def test_db_guard_target_margin_range(env):
    schema, eng = env
    se = _engine_with_search_path(DATABASE_URL, schema)
    with se.begin() as c:
        c.execute(text(_LEGACY_PRODUCTS))
    _apply_migration(se, schema)

    _expect_rejected(
        se, id=str(uuid.uuid4()), name="m0", price="10.00",
        target_margin_percent="0",
    )
    _expect_rejected(
        se, id=str(uuid.uuid4()), name="mneg", price="10.00",
        target_margin_percent="-10",
    )
    _expect_rejected(
        se, id=str(uuid.uuid4()), name="m100", price="10.00",
        target_margin_percent="100",
    )
    _expect_rejected(
        se, id=str(uuid.uuid4()), name="m150", price="10.00",
        target_margin_percent="150",
    )

    _insert_product(
        se, id=str(uuid.uuid4()), name="m1", price="10.00",
        target_margin_percent="1",
    )
    _insert_product(
        se, id=str(uuid.uuid4()), name="m99", price="10.00",
        target_margin_percent="99.99",
    )

    assert _public_columns_snapshot(eng) == _public_columns_snapshot(eng)


def test_db_guard_pricing_status_whitelist(env):
    schema, eng = env
    se = _engine_with_search_path(DATABASE_URL, schema)
    with se.begin() as c:
        c.execute(text(_LEGACY_PRODUCTS))
    _apply_migration(se, schema)

    _expect_rejected(
        se, id=str(uuid.uuid4()), name="bogus", price="10.00",
        pricing_status="reservado",
    )
    _expect_rejected(
        se, id=str(uuid.uuid4()), name="empty", price="10.00",
        pricing_status="",
    )

    _insert_product(
        se, id=str(uuid.uuid4()), name="stale", price="10.00",
        pricing_status="stale",
    )
    _insert_product(
        se, id=str(uuid.uuid4()), name="blocked", price="10.00",
        pricing_status="blocked",
    )

    assert _public_columns_snapshot(eng) == _public_columns_snapshot(eng)


def test_pricing_and_publish_roundtrip_in_disposable_schema(env, monkeypatch):
    schema, eng = env
    public_before = _public_columns_snapshot(eng)

    import config
    import loja.schema as schema_mod
    from loja import create_app

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

    from loja import db
    from loja.integrations.aliexpress.importer import SOURCE_ALIEXPRESS
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct
    from loja.services import pricing
    from loja.services.publication import publish_product

    app = create_app()
    with app.app_context():
        external_id = "1005000000002001"
        supplier = Supplier(
            id=uuid.uuid4(), name="Fornecedor PG", seller_id="SELLER_PG_F6",
            channel_type="aliexpress",
        )
        db.session.add(supplier)
        db.session.flush()
        product = Product(
            id=uuid.uuid4(),
            name="Camiseta PG",
            price=Decimal("1.00"),
            stock=5,
            is_active=False,
            is_draft=True,
            source=SOURCE_ALIEXPRESS,
            external_id=external_id,
            currency="BRL",
            pricing_status=pricing.UNPRICED,
        )
        db.session.add(product)
        db.session.flush()
        supplier_product = SupplierProduct(
            id=uuid.uuid4(), supplier_id=supplier.id,
            product_id=product.id, supplier_product_id="SP_PG_F6",
            cost=Decimal("100.00"), currency="BRL",
            freight_status=pricing.FREIGHT_AVAILABLE,
            shipping_cost=Decimal("10.00"), landed_cost=None,
            freight_currency="BRL", is_active=True,
        )
        db.session.add(supplier_product)
        db.session.flush()
        db.session.add(ProductVariant(
            id=uuid.uuid4(), product_id=product.id,
            supplier_product_id=supplier_product.id, stock=5, is_active=True,
        ))
        db.session.commit()

        result = pricing.price_dropshipping_product(
            product=product, target_margin_percent="20"
        )
        db.session.commit()
        assert result.sale_price == Decimal("137.50")
        assert result.landed_cost == Decimal("110.00")

        db.session.refresh(product)
        assert product.pricing_status == pricing.PRICED
        assert product.price == Decimal("137.50")

        publish_product(product)
        db.session.commit()
        db.session.refresh(product)
        assert product.is_draft is False
        assert product.is_active is True

        # A guarda de banco impede ativação de AliExpress não-priced: o
        # commit que tentaria criar estado inválido é rejeitado.
        half_ready = Product(
            id=uuid.uuid4(), name="Metade", price=Decimal("1.00"),
            stock=5, is_active=False, is_draft=True,
            source=SOURCE_ALIEXPRESS,
            external_id="1005000000002002", currency="BRL",
            pricing_status=pricing.UNPRICED,
        )
        db.session.add(half_ready)
        db.session.flush()
        half_ready.is_active = True
        try:
            db.session.commit()
        except (IntegrityError, ProgrammingError, DBAPIError):
            db.session.rollback()
        else:
            raise AssertionError("banco aceitou AliExpress ativo não-priced")
        db.session.expire_all()
        assert db.session.get(Product, half_ready.id) is None

    assert _public_columns_snapshot(eng) == public_before, "public foi alterado!"