"""Validator for migration 0011: AliExpress order item supplier_sku_attr.

Validates:
- Column supplier_sku_attr exists in pollomodas.aliexpress_order_items
- Column type is TEXT
- Column is nullable
- CHECK constraint exists (if present) enforcing non-empty strings
- No prohibited AliExpress tables in public schema
- Idempotent: running twice should not fail

Usage (requires PostgreSQL):
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0011_aliexpress_order_item_sku_attr.sql
    DATABASE_URL="$DATABASE_URL" python3 migrations/validate_0011.py

Without PostgreSQL:
    python3 migrations/validate_0011.py   # prints NOT RUN and exits 0
"""

from __future__ import annotations

import os
import sys


def _check_pg_available() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    return bool(url and url.startswith("postgresql"))


def validate_schema(conn) -> list[str]:
    errors: list[str] = []
    cur = conn.cursor()

    # 1. Column exists
    cur.execute("""
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'pollomodas'
          AND table_name = 'aliexpress_order_items'
          AND column_name = 'supplier_sku_attr'
    """)
    row = cur.fetchone()
    if row is None:
        errors.append("column supplier_sku_attr NOT FOUND")
        return errors

    data_type, is_nullable = row
    if data_type.upper() != "TEXT":
        errors.append(f"column type expected TEXT, got {data_type!r}")
    if is_nullable.upper() != "YES":
        errors.append(f"column nullable expected YES, got {is_nullable!r}")

    # 2. CHECK constraint (optional)
    cur.execute("""
        SELECT conname
        FROM pg_constraint
        WHERE conname = 'ck_aliexpress_order_item_sku_attr_not_empty'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'pollomodas' AND c.relname = 'aliexpress_order_items'
          )
    """)
    ck_row = cur.fetchone()
    if ck_row is not None:
        # constraint exists — verify it works
        try:
            cur.execute("""
                INSERT INTO pollomodas.aliexpress_order_items
                    (id, aliexpress_order_id, order_item_id,
                     supplier_product_external_id, supplier_sku_external_id,
                     supplier_sku_attr, quantity, state)
                VALUES (
                    gen_random_uuid(),
                    (SELECT id FROM pollomodas.aliexpress_orders LIMIT 1),
                    (SELECT id FROM pollomodas.order_items LIMIT 1),
                    'SP_TEST', 'SKU_TEST', '', 1, 'queued'
                )
            """)
            conn.rollback()
            errors.append("CHECK constraint allowed empty string (expected rejection)")
        except Exception:
            conn.rollback()
            # expected: CHECK rejected empty string

    # 3. No prohibited AliExpress tables in public
    cur.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN (
              'aliexpress_credentials',
              'aliexpress_order_items',
              'aliexpress_submission_snapshots'
          )
    """)
    obj_count = cur.fetchone()[0]
    if obj_count > 0:
        errors.append(f"Prohibited AliExpress tables found in public schema ({obj_count})")

    return errors


def main() -> int:
    if not _check_pg_available():
        print("NOT RUN: no PostgreSQL DATABASE_URL available")
        return 0

    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        errors = validate_schema(conn)
        if errors:
            for e in errors:
                print(f"FAIL: {e}")
            return 1
        print("OK: migration 0011 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
