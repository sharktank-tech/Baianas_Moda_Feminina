"""Migration 0037 (tentativas de sincronização do publicado) em PostgreSQL.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_sync_attempts_* criado e dropado; NUNCA toca public nem produção.

Cobre em PostgreSQL:
- UP 0037: tabela aliexpress_sync_attempts + índice, tudo idempotente;
- CHECK de status vinculado ao schema certo (conrelid) rejeita status inválido;
- FKs para products/supplier_products com o comportamento declarado;
- DOWN remove a tabela e o UP reexecuta limpo;
- public permanece intocado.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from conftest import ORIGINAL_DATABASE_URL
from test_draft_import_pg import _split_sql_statements
from test_pricing_publication_pg import _assert_check_violation

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

UP = "0037_aliexpress_sync_attempts.sql"
DOWN = "0037_aliexpress_sync_attempts_down.sql"
TABLE = "aliexpress_sync_attempts"
STATUS_CONSTRAINT = "ck_aliexpress_sync_attempt_status"
INDEX = "ix_aliexpress_sync_attempts_product_created"


@pytest.fixture
def pg_env():
    """Schema descartável só com as pré-requisitas da migration (sem ORM)."""
    schema = "pollo_test_sync_attempts_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))
        connection.exec_driver_sql(
            'CREATE TABLE "%s".products (id uuid PRIMARY KEY)' % schema
        )
        connection.exec_driver_sql(
            'CREATE TABLE "%s".supplier_products (id uuid PRIMARY KEY)' % schema
        )
    yield engine, schema
    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _run(engine, schema, filename):
    sql = Path("migrations", filename).read_text()
    sql = sql.replace("pollomodas.", '"%s".' % schema)
    sql = sql.replace(
        "SET search_path TO pollomodas;",
        'SET search_path TO "%s";' % schema,
    )
    with engine.begin() as connection:
        for stmt in _split_sql_statements(sql):
            connection.exec_driver_sql(stmt)


def _table_exists(engine, schema):
    with engine.begin() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = :t"
                ),
                {"s": schema, "t": TABLE},
            ).first()
        )


def _columns(engine, schema):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return {r[0] for r in rows}


def _constraint_names(engine, schema):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t)"
            ),
            {"t": '"%s".%s' % (schema, TABLE)},
        ).all()
    return {r[0] for r in rows}


def _index_names(engine, schema):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = :s AND tablename = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return {r[0] for r in rows}


def _public_has_table():
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": TABLE},
            ).first()
        return row is not None
    finally:
        engine.dispose()


def _insert(engine, schema, **values):
    columns = {
        "status": "applied",
        "variants_updated": 0,
        "variants_deactivated": 0,
        "variants_created": 0,
    }
    columns.update(values)
    placeholders = ", ".join(":%s" % name for name in columns)
    with engine.begin() as connection:
        connection.execute(
            text(
                'INSERT INTO "%s".%s (%s) VALUES (%s)'
                % (schema, TABLE, ", ".join(columns), placeholders)
            ),
            columns,
        )


def test_migration_0037_cria_tabela_idempotente(pg_env):
    engine, schema = pg_env
    public_before = _public_has_table()

    _run(engine, schema, UP)
    _run(engine, schema, UP)  # idempotente

    assert _table_exists(engine, schema)
    assert {
        "id",
        "product_id",
        "supplier_product_id",
        "status",
        "code",
        "divergence",
        "catalog_code",
        "variants_updated",
        "variants_deactivated",
        "variants_created",
        "stock_before",
        "stock_after",
        "created_at",
    }.issubset(_columns(engine, schema))

    # Constraint e índice REALMENTE vinculados ao schema descartável.
    assert STATUS_CONSTRAINT in _constraint_names(engine, schema)
    assert INDEX in _index_names(engine, schema)

    assert _public_has_table() == public_before, "public foi alterado!"


def test_migration_0037_status_check_e_fks(pg_env):
    engine, schema = pg_env
    _run(engine, schema, UP)

    import uuid

    product_id = uuid.uuid4()
    supplier_product_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text('INSERT INTO "%s".products (id) VALUES (:id)' % schema),
            {"id": product_id},
        )
        connection.execute(
            text(
                'INSERT INTO "%s".supplier_products (id) VALUES (:id)' % schema
            ),
            {"id": supplier_product_id},
        )

    # Linha válida: aplicada com contagens e códigos estáveis.
    _insert(
        engine, schema,
        product_id=product_id,
        supplier_product_id=supplier_product_id,
        status="applied",
        catalog_code="catalog_census_unproven",
        divergence="supplier_cost_changed",
        variants_updated=2,
        variants_deactivated=1,
        stock_before=8,
        stock_after=7,
    )
    # Os quatro statuses canônicos passam.
    for status in ("rejected", "failed", "api_error"):
        _insert(engine, schema, product_id=product_id, status=status)

    # Status fora do conjunto canônico é rejeitado pela CHECK do schema.
    with pytest.raises(DBAPIError) as excinfo:
        _insert(engine, schema, product_id=product_id, status="diverging")
    _assert_check_violation(excinfo.value, STATUS_CONSTRAINT)

    # FK: produto inexistente não entra (a auditoria não vira órfã).
    with pytest.raises(DBAPIError) as excinfo:
        _insert(engine, schema, product_id=uuid.uuid4())
    orig = getattr(excinfo.value, "orig", None) or excinfo.value
    sqlstate = getattr(orig, "pgcode", None)
    if sqlstate is None:
        arg0 = orig.args[0] if getattr(orig, "args", None) else None
        sqlstate = arg0.get("C") if isinstance(arg0, dict) else None
    assert sqlstate == "23503", f"SQLSTATE esperado 23503, obtido {sqlstate!r}"

    # product_id anulável: tentativa de produto já apagado sobrevive.
    _insert(engine, schema, product_id=None, status="failed")

    assert _public_has_table() is False, "public foi alterado!"


def test_migration_0037_down_remove_e_up_restaura(pg_env):
    engine, schema = pg_env
    public_before = _public_has_table()

    _run(engine, schema, UP)
    assert _table_exists(engine, schema)

    _run(engine, schema, DOWN)
    _run(engine, schema, DOWN)  # idempotente
    assert not _table_exists(engine, schema)

    _run(engine, schema, UP)
    assert _table_exists(engine, schema)
    assert STATUS_CONSTRAINT in _constraint_names(engine, schema)

    assert _public_has_table() == public_before, "public foi alterado!"
