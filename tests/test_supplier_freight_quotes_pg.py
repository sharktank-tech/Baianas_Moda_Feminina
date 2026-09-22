"""PostgreSQL real: constraint e concorrência da cotação de referência."""
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

DATABASE_URL = os.environ.get("DATABASE_URL")
try:
    _url = make_url(DATABASE_URL) if DATABASE_URL else None
    SAFE = bool(
        _url
        and _url.get_backend_name() == "postgresql"
        and _url.host in {"127.0.0.1", "localhost", "::1"}
        and str(_url.database or "").startswith("pollo")
    )
except Exception:
    SAFE = False

pytestmark = pytest.mark.skipif(not SAFE, reason="requer PostgreSQL descartável")


@pytest.fixture
def pg_quotes():
    schema = "pollo_test_reference_quote_" + secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(
            text(
                f'CREATE TABLE "{schema}".supplier_products '
                '(id uuid PRIMARY KEY)'
            )
        )
        migration = Path(
            "migrations/0025_supplier_reference_freight_quotes.sql"
        ).read_text()
        migration = migration.replace("pollomodas.", f'"{schema}".')
        conn.exec_driver_sql(migration)
    yield engine, schema
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _insert_sql(schema):
    return text(
        f"""INSERT INTO "{schema}".supplier_reference_freight_quotes
            (supplier_product_id, dest_prefix, option_key, quantity,
             currency, cost, quoted_at, expires_at, provider_method, updated_at)
            VALUES (:sp, '770', '', 1, 'BRL', :cost, :quoted, :expires,
                    'pg-read-only-test', :quoted)
            ON CONFLICT (supplier_product_id, dest_prefix, option_key, quantity)
            DO UPDATE SET cost=excluded.cost, quoted_at=excluded.quoted_at,
                          expires_at=excluded.expires_at,
                          updated_at=excluded.updated_at
            WHERE excluded.quoted_at >=
                  "{schema}".supplier_reference_freight_quotes.quoted_at"""
    )


def test_pg_window_constraint_exact_and_valid(pg_quotes):
    engine, schema = pg_quotes
    sp = uuid.uuid4()
    now = datetime(2030, 6, 15, 12, 0)
    with engine.begin() as conn:
        conn.execute(
            text(f'INSERT INTO "{schema}".supplier_products (id) VALUES (:id)'),
            {"id": sp},
        )
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(
                _insert_sql(schema),
                {"sp": sp, "cost": "1.00", "quoted": now, "expires": now},
            )
    with engine.begin() as conn:
        conn.execute(
            _insert_sql(schema),
            {
                "sp": sp,
                "cost": "2.00",
                "quoted": now,
                "expires": now + timedelta(seconds=1),
            },
        )
        assert conn.execute(
            text(
                f'SELECT count(*) FROM "{schema}".'
                "supplier_reference_freight_quotes"
            )
        ).scalar_one() == 1


def test_pg_concurrent_upsert_converges_to_one_current_row(pg_quotes):
    engine, schema = pg_quotes
    sp = uuid.uuid4()
    base = datetime(2030, 6, 15, 12, 0)
    with engine.begin() as conn:
        conn.execute(
            text(f'INSERT INTO "{schema}".supplier_products (id) VALUES (:id)'),
            {"id": sp},
        )
    barrier = threading.Barrier(2)
    errors = []

    def worker(offset, cost):
        try:
            barrier.wait(timeout=5)
            quoted = base + timedelta(seconds=offset)
            with engine.begin() as conn:
                conn.execute(
                    _insert_sql(schema),
                    {
                        "sp": sp,
                        "cost": cost,
                        "quoted": quoted,
                        "expires": quoted + timedelta(minutes=30),
                    },
                )
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [
        threading.Thread(target=worker, args=(0, "10.00")),
        threading.Thread(target=worker, args=(1, "11.00")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f'SELECT cost, quoted_at FROM "{schema}".'
                "supplier_reference_freight_quotes"
            )
        ).all()
    assert len(rows) == 1
    assert str(rows[0][0]) == "11.00"
    assert rows[0][1] == base + timedelta(seconds=1)
