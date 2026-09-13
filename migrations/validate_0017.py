"""Validator da migration 0017: ledger de supplier refund."""
from __future__ import annotations
import os
import sys


def validate_schema(conn):
    errors = []
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema='pollomodas' AND table_name='orders'
          AND column_name IN (
            'supplier_refund_state','supplier_refund_requested_at',
            'supplier_refund_confirmed_at','supplier_refund_last_error'
          )
    """)
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    expected = {
        'supplier_refund_state', 'supplier_refund_requested_at',
        'supplier_refund_confirmed_at', 'supplier_refund_last_error'
    }
    errors.extend(
        f"column {name} NOT FOUND" for name in sorted(expected - set(columns))
    )
    for name in ('supplier_refund_state', 'supplier_refund_last_error'):
        if name in columns:
            dtype, length, nullable = columns[name]
            expected_length = 30 if name.endswith('state') else 64
            if dtype != 'character varying' or length != expected_length or nullable != 'YES':
                errors.append(f"{name} has unexpected definition")
    cur.execute("""
        SELECT 1 FROM pg_constraint
        WHERE conname='ck_orders_supplier_refund_state'
          AND conrelid='pollomodas.orders'::regclass
    """)
    if cur.fetchone() is None:
        errors.append("constraint ck_orders_supplier_refund_state NOT FOUND")
    cur.execute("""
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema='public' AND table_name='orders'
          AND column_name LIKE 'supplier_refund_%'
    """)
    if cur.fetchone()[0]:
        errors.append("supplier refund columns exist in public.orders")
    return errors


def main():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("NOT RUN: no PostgreSQL DATABASE_URL available")
        return 0
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        errors = validate_schema(conn)
        if errors:
            for error in errors:
                print(f"FAIL: {error}")
            return 1
        print("OK: migration 0017 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
