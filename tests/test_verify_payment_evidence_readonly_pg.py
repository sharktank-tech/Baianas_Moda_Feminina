"""Testes PG do script verify_payment_evidence_readonly (dados sintéticos).

Schema descartável; o script roda somente leitura com POLLOMODAS_SCHEMA.
Nunca toca em `public`/`pollomodas` de produção.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
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


def _run(schema: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = DATABASE_URL
    env["BAIANAS_DB_SCHEMA"] = schema
    env["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [
                os.getcwd(),
                env.get("PYTHONPATH", ""),
            ],
        )
    )
    return subprocess.run(
        [sys.executable, "scripts/verify_payment_evidence_readonly.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def audit_schema():
    schema = f"pollo_test_payev_{secrets.token_hex(4)}"
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(
            text(
                f"""
                CREATE TABLE {schema}.payments (
                  id uuid PRIMARY KEY,
                  order_id uuid,
                  provider varchar(30) NOT NULL,
                  status varchar(50) NOT NULL,
                  amount numeric(10, 2),
                  currency varchar(3),
                  raw_payload json
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TABLE {schema}.products (
                  id uuid PRIMARY KEY,
                  name varchar,
                  price numeric(10, 2),
                  is_active boolean DEFAULT true,
                  is_draft boolean DEFAULT false
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TABLE {schema}.supplier_products (
                  id uuid PRIMARY KEY,
                  product_id uuid,
                  landed_cost numeric(10, 2),
                  is_active boolean DEFAULT true
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TABLE {schema}.product_variants (
                  id uuid PRIMARY KEY,
                  product_id uuid,
                  stock integer,
                  is_active boolean DEFAULT true
                )
                """
            )
        )
        conn.commit()
    yield schema
    with eng.connect() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()


def _insert_payment(conn, schema: str, *, payload_json: str | None, provider="mercado_pago", status="approved", amount="90.00", currency="BRL"):
    import uuid

    pid = uuid.uuid4()
    if payload_json is None:
        conn.execute(
            text(
                f"""
                INSERT INTO {schema}.payments
                  (id, order_id, provider, status, amount, currency, raw_payload)
                VALUES (:id, :oid, :p, :s, :a, :c, NULL)
                """
            ),
            {
                "id": pid,
                "oid": uuid.uuid4(),
                "p": provider,
                "s": status,
                "a": amount,
                "c": currency,
            },
        )
    else:
        conn.execute(
            text(
                f"""
                INSERT INTO {schema}.payments
                  (id, order_id, provider, status, amount, currency, raw_payload)
                VALUES (:id, :oid, :p, :s, :a, :c, CAST(:payload AS json))
                """
            ),
            {
                "id": pid,
                "oid": uuid.uuid4(),
                "p": provider,
                "s": status,
                "a": amount,
                "c": currency,
                "payload": payload_json,
            },
        )


def _seed_products(conn, schema: str):
    import uuid

    pid = uuid.uuid4()
    conn.execute(
        text(
            f"""
            INSERT INTO {schema}.products (id, name, price, is_active, is_draft)
            VALUES (:id, 'Prod', 100.00, true, false)
            """
        ),
        {"id": pid},
    )
    conn.execute(
        text(
            f"""
            INSERT INTO {schema}.supplier_products
              (id, product_id, landed_cost, is_active)
            VALUES (:id, :pid, 70.00, true)
            """
        ),
        {"id": uuid.uuid4(), "pid": pid},
    )
    conn.execute(
        text(
            f"""
            INSERT INTO {schema}.product_variants (id, product_id, stock, is_active)
            VALUES (:id, :pid, 5, true)
            """
        ),
        {"id": uuid.uuid4(), "pid": pid},
    )


def test_empty_tables_skip_and_no_pii(audit_schema):
    result = _run(audit_schema)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: SKIP" in result.stdout
    assert "INCONCLUSIVA" in result.stdout
    assert "P1 OK" not in result.stdout
    assert "total_payments: 0" in result.stdout
    assert "total_products: 0" in result.stdout
    # sem vazamento de SQL/credencial
    assert "postgresql://" not in result.stdout
    assert "password" not in result.stdout.lower()


def test_valid_installments_integer_and_aggregates(audit_schema):
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        _insert_payment(conn, audit_schema, payload_json='{"installments": 1}')
        _insert_payment(conn, audit_schema, payload_json='{"installments": 6}')
        _insert_payment(conn, audit_schema, payload_json='{"installments": 12}')
        _seed_products(conn, audit_schema)
        conn.commit()

    result = _run(audit_schema)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: OK" in result.stdout
    assert "total_payments: 3" in result.stdout
    assert "valid_installments_gte_1: 3" in result.stdout
    assert "mp_approved_valid_installments: 3" in result.stdout
    assert "installments_min: 1" in result.stdout
    assert "installments_max: 12" in result.stdout
    assert "with_positive_price: 1" in result.stdout
    assert "with_landed_cost: 1" in result.stdout
    assert "variants_in_stock: 1" in result.stdout


def test_invalid_evidence_types_fail_closed_audit(audit_schema):
    eng = create_engine(DATABASE_URL)
    cases = [
        '{"installments": "3"}',
        '{"installments": true}',
        '{"installments": false}',
        '{"installments": 0}',
        '{"installments": -1}',
        '{"installments": 1.5}',
        '{"installments": null}',
        "{}",
        "null",
    ]
    with eng.connect() as conn:
        for payload in cases:
            _insert_payment(conn, audit_schema, payload_json=payload)
        conn.commit()

    result = _run(audit_schema)
    # CASE WHEN intercepta casts inválidos: não pode ser exit 2 (erro SQL)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "status: FAIL" in result.stdout
    assert "valid_installments_gte_1: 0" in result.stdout
    assert "mp_approved_valid_installments: 0" in result.stdout
    assert "total_payments: 9" in result.stdout
    assert "consulta de auditoria" not in result.stderr


def test_zero_mp_approved_valid_with_type_valid_rows_is_fail(audit_schema):
    """Revisão P1: OK exige mp_approved_valid_installments > 0.

    Aqui há installments inteiros válidos, mas só em pending ou provider
    não-MP → deve falhar (exit 1), nunca declarar OK.
    """
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        _insert_payment(
            conn,
            audit_schema,
            payload_json='{"installments": 6}',
            status="pending",
        )
        _insert_payment(
            conn,
            audit_schema,
            payload_json='{"installments": 3}',
            provider="other",
            status="approved",
        )
        conn.commit()

    result = _run(audit_schema)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "status: FAIL" in result.stdout
    assert "P1 OK" not in result.stdout
    assert "mp_approved_valid_installments: 0" in result.stdout
    assert "valid_installments_gte_1: 2" in result.stdout
    assert "mp_approved_valid_installments = 0" in result.stderr


def test_sql_survives_string_and_null_installments_without_query_error(
    audit_schema,
):
    """Strings/nulas no cast: CASE protege; script não aborta com exit 2."""
    eng = create_engine(DATABASE_URL)
    payloads = [
        '{"installments": "abc"}',
        '{"installments": ""}',
        '{"installments": null}',
        '{"installments": [1, 2]}',
        '{"installments": {"n": 1}}',
        "null",
        "{}",
        '{"installments": "3"}',
        '{"installments": 1.5}',
    ]
    with eng.connect() as conn:
        for payload in payloads:
            _insert_payment(conn, audit_schema, payload_json=payload)
        conn.commit()

    result = _run(audit_schema)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "status: FAIL" in result.stdout
    assert "consulta de auditoria" not in result.stderr
    assert "invalid input syntax" not in result.stderr
    assert "mp_approved_valid_installments: 0" in result.stdout



def test_mixed_evidence_counts_only_valid(audit_schema):
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        _insert_payment(conn, audit_schema, payload_json='{"installments": 1}')
        _insert_payment(conn, audit_schema, payload_json='{"installments": "3"}')
        _insert_payment(
            conn,
            audit_schema,
            payload_json='{"installments": 10}',
            status="pending",
        )
        _insert_payment(
            conn,
            audit_schema,
            payload_json='{"installments": 2}',
            provider="other",
        )
        conn.commit()

    result = _run(audit_schema)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: OK" in result.stdout
    assert "total_payments: 4" in result.stdout
    assert "mp_payments: 3" in result.stdout
    assert "mp_approved_payments: 2" in result.stdout
    assert "with_installments_key: 4" in result.stdout
    # tipo-válido independe de provider/status: 1, 10 e 2 ("3" string é inválido)
    assert "valid_installments_gte_1: 3" in result.stdout
    # apenas approved+MP com evidência válida: installments 1 (o 10 é pending)
    assert "mp_approved_valid_installments: 1" in result.stdout
    assert "installments_min: 1" in result.stdout
    assert "installments_max: 10" in result.stdout


def test_large_integer_does_not_abort(audit_schema):
    eng = create_engine(DATABASE_URL)
    big = "99999999999999999999"
    with eng.connect() as conn:
        _insert_payment(
            conn,
            audit_schema,
            payload_json=f'{{"installments": {big}}}',
        )
        conn.commit()

    result = _run(audit_schema)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: OK" in result.stdout
    assert f"installments_max: {big}" in result.stdout


def test_readonly_preserves_data_and_schema(audit_schema):
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        _insert_payment(conn, audit_schema, payload_json='{"installments": 1}')
        _seed_products(conn, audit_schema)
        conn.commit()

    before = None
    with eng.connect() as conn:
        before = conn.execute(
            text(f"SELECT count(*) FROM {audit_schema}.payments")
        ).scalar()

    result = _run(audit_schema)
    assert result.returncode == 0, result.stdout + result.stderr

    with eng.connect() as conn:
        after = conn.execute(
            text(f"SELECT count(*) FROM {audit_schema}.payments")
        ).scalar()
        tables = conn.execute(
            text(
                f"SELECT count(*) FROM information_schema.tables "
                f"WHERE table_schema = '{audit_schema}'"
            )
        ).scalar()

    assert before == after == 1
    assert tables == 4


def test_missing_database_url_exits_2(audit_schema):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["POLLOMODAS_SCHEMA"] = audit_schema
    result = subprocess.run(
        [sys.executable, "scripts/verify_payment_evidence_readonly.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "DATABASE_URL" in result.stderr


def test_connection_refused_exits_2_sanitized(audit_schema):
    """engine.connect() falhando (host morto) → exit 2, sem senha na saída."""
    secret = "connect-fail-secret-pass"
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+psycopg2://user:{secret}@127.0.0.1:1/pollotest"
    )
    env["POLLOMODAS_SCHEMA"] = audit_schema
    result = subprocess.run(
        [sys.executable, "scripts/verify_payment_evidence_readonly.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert secret not in combined
    assert "postgresql://" not in combined
    assert "ERRO" in result.stderr
    assert "conexao" in result.stderr.lower() or "falha" in result.stderr.lower()


def test_forbidden_schema_rejected(audit_schema):
    env = os.environ.copy()
    env["DATABASE_URL"] = DATABASE_URL
    env["POLLOMODAS_SCHEMA"] = "public"
    result = subprocess.run(
        [sys.executable, "scripts/verify_payment_evidence_readonly.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "public" in result.stderr
