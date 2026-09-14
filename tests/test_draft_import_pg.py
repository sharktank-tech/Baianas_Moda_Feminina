"""Validação do import de rascunho AliExpress em PostgreSQL (Fase 5).

Só roda quando DATABASE_URL aponta para PostgreSQL. Usa schemas descartáveis
`pollo_test_*` e NUNCA toca `public` nem o schema persistente `pollomodas`:
- aplica migrations/0004 sem destrutivo, idempotente (2x), colunas + checks
- import/reimport integral de um draft no schema descartável
- `public` permanece intocado (snapshot antes/depois)
"""
import pathlib
import secrets

import pytest
from sqlalchemy import create_engine, text, inspect as sa_inspect
from sqlalchemy.engine import make_url

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

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "0004_aliexpress_draft_import.sql"
)


def _split_sql_statements(sql):
    """Divide o arquivo de migration em statements simples.

    Trata quoting dollar (DO $$ ... $$;): os `;` internos ao corpo não são
    separadores. Ignora comentários e comandos psql (\\set...).
    """
    out = []
    current = []
    in_dollar = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith("\\"):
            continue
        current.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and line.rstrip().endswith(";"):
            out.append("\n".join(current))
            current = []
    if current:
        out.append("\n".join(current))
    return out


def _engine_with_search_path(url, schema):
    """Engine com search_path isolado ao schema descartável para PostgreSQL.

    IMPORTANTE: passar `-csearch_path=...` via options (na URL ou em
    connect_args) é silenciosamente ignorado pelo pooler do Supabase
    (Supavisor), fazendo o CREATE TABLE/INSERT vazar para `public`.
    O caminho confiável é executar `SET search_path` logo após a conexão.
    """
    engine = create_engine(url)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("SET search_path = %s" % schema)
        cur.close()

    return engine


def _public_columns_snapshot(eng):
    """Snapshot de TODAS as tabelas/colunas de `public` (invariante intocável)."""
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT table_name, column_name, data_type "
                "FROM   information_schema.columns "
                "WHERE  table_schema = 'public' "
                "ORDER  BY table_name, ordinal_position"
            )
        ).fetchall()
    return [tuple(r) for r in rows]


def _apply_migration(engine, schema):
    sql = _MIGRATION.read_text()
    sql = sql.replace(
        "SET search_path TO pollomodas;",
        "SET search_path TO %s;" % schema,
    )
    sql = sql.replace(
        "'pollomodas.supplier_products'::regclass",
        "'%s.supplier_products'::regclass" % schema,
    )
    with engine.begin() as c:
        for stmt in _split_sql_statements(sql):
            c.execute(text(stmt))


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_%s_%s" % ("draft", secrets.token_hex(4))
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % schema))
        c.commit()

    yield schema, eng

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % schema))
        c.commit()


def test_migration_is_idempotent_and_public_untouched(env):
    schema, eng = env
    before = _public_columns_snapshot(eng)

    se = _engine_with_search_path(DATABASE_URL, schema)
    # A migration é aditiva sobre tabelas EXISTENTES: cria a forma "legada"
    # (sem as colunas novas) para validar os ADD COLUMN/CONSTRAINT de fato.
    with se.begin() as c:
        c.execute(
            text(
                "CREATE TABLE supplier_products ("
                "  id UUID PRIMARY KEY NOT NULL,"
                "  supplier_id UUID,"
                "  supplier_product_id VARCHAR,"
                "  product_id UUID,"
                "  cost NUMERIC(12, 2),"
                "  currency VARCHAR(3)"
                ")"
            )
        )
        c.execute(
            text(
                "CREATE TABLE product_variants ("
                "  id UUID PRIMARY KEY NOT NULL,"
                "  product_id UUID,"
                "  supplier_product_id UUID,"
                "  supplier_sku_id VARCHAR"
                ")"
            )
        )
    _apply_migration(se, schema)
    _apply_migration(se, schema)  # idempotente

    inspector = sa_inspect(se)
    sp_cols = {c["name"] for c in inspector.get_columns("supplier_products", schema=schema)}
    pv_cols = {c["name"] for c in inspector.get_columns("product_variants", schema=schema)}
    assert {
        "freight_status", "shipping_cost", "landed_cost", "freight_currency",
    }.issubset(sp_cols)
    assert "attributes" in pv_cols

    with se.connect() as c:
        connames = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = to_regnamespace(:schema)::oid"
                ),
                {"schema": schema},
            )
        }
    assert "ck_supplier_product_shipping_cost_nonneg" in connames
    assert "ck_supplier_product_landed_cost_nonneg" in connames

    # As constraints são REAIS: frete negativo é rejeitado.
    with se.connect() as c:
        c.execute(
            text(
                "INSERT INTO supplier_products "
                "(id, supplier_id, product_id, freight_status, shipping_cost) "
                "VALUES (gen_random_uuid(), NULL, NULL, 'safe_null', NULL)"
            )
        )
        c.commit()
        try:
            c.execute(
                text(
                    "INSERT INTO supplier_products "
                    "(id, freight_status, shipping_cost) "
                    "VALUES (gen_random_uuid(), 'provider_unavailable', -1.00)"
                )
            )
        except Exception:
            c.rollback()
        else:
            raise AssertionError("CHECK shipping_cost >= 0 não está ativo")

    assert _public_columns_snapshot(eng) == before, "public foi alterado!"


def test_import_reimport_end_to_end_in_disposable_schema(env, monkeypatch):
    schema, eng = env

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
    from loja.integrations.aliexpress.importer import import_product_draft
    from loja.integrations.aliexpress.models import (
        AliExpressProductDetail,
        AliExpressProductSku,
    )
    from loja.modules.models import Product, SupplierProduct, Supplier

    external_id = "1005000000001001"
    detail = AliExpressProductDetail(
        product_id=external_id,
        title="Camiseta PG",
        description="desc",
        category_id="200003959",
        images=["https://pg/a.jpg", "https://pg/a.jpg", "https://pg/b.jpg"],
        skus=[
            AliExpressProductSku(
                sku_id="1", sale_price="10.00", original_price="19.90",
                available_stock=5, currency="USD",
            ),
            AliExpressProductSku(
                sku_id="2", sale_price="15.00", original_price="25.00",
                available_stock=3, currency="USD",
            ),
            # sku_id duplicado no MESMO payload: a primeira ocorrência vence
            # (uma única variante; nunca viola uq_product_variant_supplier).
            AliExpressProductSku(
                sku_id="1", sale_price="99.00", original_price="199.00",
                available_stock=9, currency="USD",
            ),
        ],
        currency="USD",
    )

    public_before = _public_columns_snapshot(eng)

    app = create_app()
    with app.app_context():
        inspector = sa_inspect(db.engine)
        new_cols = {
            c["name"] for c in inspector.get_columns("supplier_products", schema=schema)
        }
        assert "freight_status" in new_cols  # ensure_schema aditivo no schema novo

        first = import_product_draft(detail=detail, seller_id="SELLER_PG")
        assert first.created is True
        assert first.variants_created == 2  # duplicado deduplicado
        assert first.images == 2

        second = import_product_draft(detail=detail, seller_id="SELLER_PG")
        assert second.created is False
        assert second.variants_created == 0
        assert second.variants_updated == 2

        assert Product.query.filter_by(
            source="aliexpress", external_id=external_id
        ).count() == 1
        assert Supplier.query.filter_by(seller_id="SELLER_PG").count() == 1
        assert SupplierProduct.query.count() == 1

        supplier_product = SupplierProduct.query.one()
        assert supplier_product.freight_status == "provider_unavailable"
        assert supplier_product.shipping_cost is None
        assert supplier_product.landed_cost is None
        assert supplier_product.currency == "USD"

    assert _public_columns_snapshot(eng) == public_before, "public foi alterado!"