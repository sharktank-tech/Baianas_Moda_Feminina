"""Migration 0038 (estoque do fornecedor por variante) em PostgreSQL.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_variant_supplier_stock_* criado e dropado; NUNCA toca public
nem produção.

Cobre em PostgreSQL:
- UP 0038: coluna product_variants.supplier_stock aditiva e idempotente;
- a coluna nasce NULL sem backfill (só a sincronização preenche);
- DOWN remove a coluna e o UP reexecuta limpo;
- public permanece intocado.

A coluna é o TETO da liberação de reserva (revisão PR #123): sem ela no
banco, o teto não existe e a liberação volta a inflar o estoque acima do
que o fornecedor informa.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL
from test_draft_import_pg import _split_sql_statements

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

UP = "0038_variant_supplier_stock.sql"
DOWN = "0038_variant_supplier_stock_down.sql"
TABLE = "product_variants"
COLUMN = "supplier_stock"


@pytest.fixture
def pg_env():
    """Schema descartável só com a pré-requisita da migration (sem ORM)."""
    schema = "pollo_test_variant_stock_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))
        connection.exec_driver_sql(
            'CREATE TABLE "%s".product_variants '
            "(id uuid PRIMARY KEY, stock integer NOT NULL DEFAULT 0)" % schema
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


def _column(engine, schema):
    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t "
                "AND column_name = :c"
            ),
            {"s": schema, "t": TABLE, "c": COLUMN},
        ).first()


def test_migration_0038_adiciona_coluna_idempotente(pg_env):
    engine, schema = pg_env
    assert _column(engine, schema) is None

    _run(engine, schema, UP)
    first = _column(engine, schema)
    assert first is not None
    data_type, is_nullable, column_default = first
    # Aditiva: aceita o espelho e nenhum valor é forçado (sem backfill).
    assert data_type == "integer"
    assert is_nullable == "YES"
    assert column_default is None

    # Idempotente: reexecutar não duplica nem erro.
    _run(engine, schema, UP)
    assert _column(engine, schema) == first


def test_migration_0038_down_remove_e_up_restaura(pg_env):
    engine, schema = pg_env
    _run(engine, schema, UP)
    assert _column(engine, schema) is not None

    _run(engine, schema, DOWN)
    assert _column(engine, schema) is None

    _run(engine, schema, UP)
    assert _column(engine, schema) is not None


def test_migration_0038_nao_toca_no_schema_public(pg_env):
    engine, schema = pg_env
    _run(engine, schema, UP)
    _run(engine, schema, DOWN)

    with engine.begin() as connection:
        public_column = connection.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = :c"
            ),
            {"t": TABLE, "c": COLUMN},
        ).first()
    assert public_column is None
