"""Etapa 1 — migration 0034 (origem de envio com evidência) em PostgreSQL.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_shipping_origin_* criado e dropado; NUNCA toca public nem produção.

Cobre em PostgreSQL (o dialeto real de produção):
- UP 0034: tabela supplier_sku_shipping_origins + coluna
  product_variants.national_shipping_operation (default FALSE) + CHECKs +
  índices únicos parciais, tudo idempotente (reaplicar não quebra);
- CHECKs rejeitam origem manual sem evidência, origem inválida, escopo não
  XOR, país inválido e linha duplicada por escopo;
- DOWN 0034: remove coluna/tabela/índices; UP reexecuta limpo.
"""

import secrets
import uuid
from pathlib import Path

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

UP = "0034_shipping_origin_evidence.sql"
DOWN = "0034_shipping_origin_evidence_down.sql"

TABLE = "supplier_sku_shipping_origins"
ORIGIN_CONSTRAINTS = (
    "ck_shipping_origin_scope",
    "ck_shipping_origin_api_origin",
    "ck_shipping_origin_manual_origin",
    "ck_shipping_origin_manual_evidence",
    "ck_shipping_origin_api_evidence",
    "ck_shipping_origin_country",
)
ORIGIN_INDEXES = (
    "uq_shipping_origin_variant_sku",
    "uq_shipping_origin_cover_all",
)


@pytest.fixture
def pg_env():
    """Schema descartável com apenas os pré-requisitos da migration.

    Propositalmente SEM db.create_all(): a migration precisa criar a tabela
    sozinha, como em um banco de produção que ainda não a possui.
    """
    schema = "pollo_test_shipping_origin_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))
        connection.exec_driver_sql(
            'CREATE TABLE "%s".users (id uuid PRIMARY KEY)' % schema
        )
        connection.exec_driver_sql(
            'CREATE TABLE "%s".supplier_products (id uuid PRIMARY KEY)' % schema
        )
        connection.exec_driver_sql(
            'CREATE TABLE "%s".product_variants ('
            "id uuid PRIMARY KEY,"
            "supplier_product_id uuid,"
            "supplier_sku_id varchar(64)"
            ")" % schema
        )
    yield engine, schema
    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _migration_sql(filename, schema):
    return (
        Path("migrations", filename)
        .read_text()
        .replace("pollomodas.", '"%s".' % schema)
    )


def _run_migration(engine, schema, filename):
    with engine.begin() as connection:
        connection.exec_driver_sql(_migration_sql(filename, schema))


def _table_exists(engine, schema, table):
    with engine.begin() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = :t"
                ),
                {"s": schema, "t": table},
            ).first()
        )


def _columns(engine, schema, table):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": schema, "t": table},
        ).all()
    return {r[0] for r in rows}


def _constraints(engine, schema, table):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t)"
            ),
            {"t": '"%s".%s' % (schema, table)},
        ).all()
    return {r[0] for r in rows}


def _indexes(engine, schema, table):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = :s AND tablename = :t "
                "AND indexname LIKE 'uq_shipping_origin%'"
            ),
            {"s": schema, "t": table},
        ).all()
    return {r[0] for r in rows}


def _seed(engine, schema):
    supplier_product_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text('INSERT INTO "%s".supplier_products (id) VALUES (:id)' % schema),
            {"id": supplier_product_id},
        )
        connection.execute(
            text(
                'INSERT INTO "%s".product_variants '
                "(id, supplier_product_id, supplier_sku_id) "
                "VALUES (:id, :sp, :sku)" % schema
            ),
            {"id": variant_id, "sp": supplier_product_id, "sku": "SKU-PG-1"},
        )
    return supplier_product_id, variant_id


def _insert_origin(engine, schema, *, supplier_product_id, **values):
    columns = {
        "supplier_product_id": supplier_product_id,
        "supplier_sku_id": None,
        "covers_all_variants": False,
        "api_origin": None,
        "manual_origin": None,
        "origin_country": None,
        "evidence_ref": None,
        "confirmed_by_user_id": None,
        "confirmed_at": None,
        "api_observed_at": None,
    }
    columns.update(values)
    placeholders = ", ".join(":%s" % name for name in columns)
    with engine.begin() as connection:
        connection.execute(
            text(
                'INSERT INTO "%s".%s (%s) VALUES (%s)'
                % (
                    schema,
                    TABLE,
                    ", ".join(columns),
                    placeholders,
                )
            ),
            columns,
        )


def test_migration_0034_creates_structure_idempotent(pg_env):
    engine, schema = pg_env
    _seed(engine, schema)

    _run_migration(engine, schema, UP)

    assert _table_exists(engine, schema, TABLE)
    columns = _columns(engine, schema, TABLE)
    for column in (
        "id",
        "supplier_product_id",
        "supplier_sku_id",
        "covers_all_variants",
        "api_origin",
        "manual_origin",
        "origin_country",
        "evidence_ref",
        "confirmed_by_user_id",
        "confirmed_at",
        "api_observed_at",
    ):
        assert column in columns

    constraints = _constraints(engine, schema, TABLE)
    for constraint in ORIGIN_CONSTRAINTS:
        assert constraint in constraints
    for index in ORIGIN_INDEXES:
        assert index in _indexes(engine, schema, TABLE)

    variant_columns = _columns(engine, schema, "product_variants")
    assert "national_shipping_operation" in variant_columns

    # Coluna nova nasce FALSE para as linhas já existentes (sem backfill).
    with engine.begin() as connection:
        value = connection.execute(
            text(
                "SELECT national_shipping_operation FROM "
                '"%s".product_variants' % schema
            )
        ).scalar()
    assert value is False

    # Idempotente: reaplicar o UP não falha nem altera o default.
    _run_migration(engine, schema, UP)
    _run_migration(engine, schema, UP)
    with engine.begin() as connection:
        default = connection.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = :s "
                "AND table_name = 'product_variants' "
                "AND column_name = 'national_shipping_operation'"
            ),
            {"s": schema},
        ).scalar()
    assert default is not None
    assert "false" in default.lower()


def test_migration_0034_checks_reject_bad_rows(pg_env):
    engine, schema = pg_env
    supplier_product_id, _variant_id = _seed(engine, schema)
    _run_migration(engine, schema, UP)

    base = {"supplier_product_id": supplier_product_id}

    # origem manual sem evidência
    with pytest.raises(Exception):
        _insert_origin(
            engine, schema,
            covers_all_variants=True,
            manual_origin="brazil",
            **base,
        )
    # origem fora do conjunto canônico
    with pytest.raises(Exception):
        _insert_origin(
            engine, schema,
            covers_all_variants=True,
            api_origin="us",
            api_observed_at="2030-06-15 10:00:00",
            **base,
        )
    # escopo não XOR (SKU + cobertura na mesma linha)
    with pytest.raises(Exception):
        _insert_origin(
            engine, schema,
            supplier_sku_id="SKU-PG-1",
            covers_all_variants=True,
            api_origin="foreign",
            api_observed_at="2030-06-15 10:00:00",
            **base,
        )
    # país inválido
    with pytest.raises(Exception):
        _insert_origin(
            engine, schema,
            covers_all_variants=True,
            api_origin="foreign",
            api_observed_at="2030-06-15 10:00:00",
            origin_country="BRA",
            **base,
        )
    # linha válida passa
    _insert_origin(
        engine, schema,
        covers_all_variants=True,
        manual_origin="brazil",
        evidence_ref="PROVA-PG-1",
        confirmed_at="2030-06-15 10:00:00",
        origin_country="BR",
        **base,
    )
    # duplicata no mesmo escopo (índice único parcial) é recusada
    with pytest.raises(Exception):
        _insert_origin(
            engine, schema,
            covers_all_variants=True,
            api_origin="foreign",
            api_observed_at="2030-06-15 10:00:00",
            **base,
        )
    # SKU distinto convive na mesma tabela (outro índice parcial)
    _insert_origin(
        engine, schema,
        supplier_sku_id="SKU-PG-2",
        manual_origin="brazil",
        evidence_ref="PROVA-PG-2",
        confirmed_at="2030-06-15 10:00:00",
        **base,
    )


def test_migration_0034_down_removes_and_up_restores(pg_env):
    engine, schema = pg_env
    _seed(engine, schema)
    _run_migration(engine, schema, UP)

    _run_migration(engine, schema, DOWN)

    assert not _table_exists(engine, schema, TABLE)
    assert "national_shipping_operation" not in _columns(
        engine, schema, "product_variants"
    )
    for index in ORIGIN_INDEXES:
        assert index not in _indexes(engine, schema, TABLE)

    # Recuperar: UP reexecuta limpo após o DOWN.
    _run_migration(engine, schema, UP)
    assert _table_exists(engine, schema, TABLE)
    assert "national_shipping_operation" in _columns(
        engine, schema, "product_variants"
    )
    for constraint in ORIGIN_CONSTRAINTS:
        assert constraint in _constraints(engine, schema, TABLE)


# --------------------------------------------------------------------------
# Migration 0036 — snapshot de SKUs cobertos pela confirmação coletiva
# --------------------------------------------------------------------------

UP_COVER = "0036_shipping_origin_cover_snapshot.sql"
DOWN_COVER = "0036_shipping_origin_cover_snapshot_down.sql"
COVER_COLUMN = "covered_supplier_sku_ids"


def _run_migration_v2(engine, schema, filename):
    r"""Migration com `\set` + `SET search_path` (0036 em diante)."""
    from test_draft_import_pg import _split_sql_statements

    sql = Path("migrations", filename).read_text()
    sql = sql.replace("pollomodas.", '"%s".' % schema)
    sql = sql.replace(
        "SET search_path TO pollomodas;",
        'SET search_path TO "%s";' % schema,
    )
    with engine.begin() as connection:
        for stmt in _split_sql_statements(sql):
            connection.exec_driver_sql(stmt)


def _public_has_cover_column():
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'supplier_sku_shipping_origins' "
                    "AND column_name = :c"
                ),
                {"c": COVER_COLUMN},
            ).first()
        return row is not None
    finally:
        engine.dispose()


def test_migration_0036_adiciona_snapshot_idempotente(pg_env):
    engine, schema = pg_env
    supplier_product_id, _variant_id = _seed(engine, schema)
    public_before = _public_has_cover_column()

    _run_migration(engine, schema, UP)
    _run_migration_v2(engine, schema, UP_COVER)
    _run_migration_v2(engine, schema, UP_COVER)  # idempotente

    assert COVER_COLUMN in _columns(engine, schema, TABLE)

    # Coluna nasce NULL (sem backfill) e persiste a lista coberta.
    _insert_origin(
        engine, schema,
        supplier_product_id=supplier_product_id,
        covers_all_variants=True,
        manual_origin="brazil",
        evidence_ref="PROVA-COB-1",
        confirmed_at="2030-06-15 10:00:00",
        origin_country="BR",
        covered_supplier_sku_ids='["SKU-PG-1"]',
    )
    with engine.begin() as connection:
        value = connection.execute(
            text(
                "SELECT %s FROM \"%s\".%s"
                % (COVER_COLUMN, schema, TABLE)
            )
        ).scalar()
    assert value == '["SKU-PG-1"]'

    # Linha sem snapshot continua NULL (fail-closed na resolução).
    with engine.begin() as connection:
        nulls = connection.execute(
            text(
                "SELECT count(*) FROM \"%s\".%s "
                "WHERE covers_all_variants = TRUE AND %s IS NULL"
                % (schema, TABLE, COVER_COLUMN)
            )
        ).scalar()
    assert nulls == 0

    assert _public_has_cover_column() == public_before, "public foi alterado!"


def test_migration_0036_down_remove_e_up_restaura(pg_env):
    engine, schema = pg_env
    _seed(engine, schema)
    public_before = _public_has_cover_column()

    _run_migration(engine, schema, UP)
    _run_migration_v2(engine, schema, UP_COVER)
    assert COVER_COLUMN in _columns(engine, schema, TABLE)

    _run_migration_v2(engine, schema, DOWN_COVER)
    _run_migration_v2(engine, schema, DOWN_COVER)  # idempotente
    assert COVER_COLUMN not in _columns(engine, schema, TABLE)

    _run_migration_v2(engine, schema, UP_COVER)
    assert COVER_COLUMN in _columns(engine, schema, TABLE)

    assert _public_has_cover_column() == public_before, "public foi alterado!"
