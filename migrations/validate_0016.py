"""Validator da migration 0016: tracking em aliexpress_remote_orders."""
from __future__ import annotations
import os
import sys


def validate_schema(conn) -> list[str]:
    errors = []
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema='baianas_moda_feminina'
          AND table_name='aliexpress_remote_orders'
          AND column_name IN ('tracking_code','carrier','tracking_updated_at')
    """)
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    expected = {'tracking_code', 'carrier', 'tracking_updated_at'}
    missing = expected - set(columns)
    errors.extend(f"column {name} NOT FOUND" for name in sorted(missing))
    for name in ('tracking_code', 'carrier'):
        if name in columns:
            dtype, length, nullable = columns[name]
            if dtype != 'character varying' or length != 64 or nullable != 'YES':
                errors.append(f"{name} has unexpected definition")
    if 'tracking_updated_at' in columns:
        dtype, _, nullable = columns['tracking_updated_at']
        if dtype != 'timestamp without time zone' or nullable != 'YES':
            errors.append("tracking_updated_at has unexpected definition")
    cur.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid='baianas_moda_feminina.aliexpress_remote_orders'::regclass
          AND conname IN ('ck_areo_tracking_code_format','ck_areo_carrier_format')
    """)
    constraints = {row[0] for row in cur.fetchall()}
    for name in ('ck_areo_tracking_code_format','ck_areo_carrier_format'):
        if name not in constraints:
            errors.append(f"constraint {name} NOT FOUND")
    cur.execute("""
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name='aliexpress_remote_orders'
          AND column_name IN ('tracking_code','carrier','tracking_updated_at')
    """)
    if cur.fetchone()[0]:
        errors.append("tracking columns unexpectedly exist in public schema")
    return errors


def main() -> int:
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
        print("OK: migration 0016 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
