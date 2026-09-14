"""Validator da migration 0018: origem de tráfego no pedido (FASE 9.0.0)."""
from __future__ import annotations
import os


def validate_schema(conn):
    errors = []
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema='pollomodas' AND table_name='orders'
          AND column_name IN (
            'acquisition_source','acquisition_medium',
            'acquisition_campaign','acquisition_referrer'
          )
    """)
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    expected = {
        'acquisition_source', 'acquisition_medium',
        'acquisition_campaign', 'acquisition_referrer',
    }
    errors.extend(
        f"column {name} NOT FOUND" for name in sorted(expected - set(columns))
    )
    expected_lengths = {
        'acquisition_source': 64,
        'acquisition_medium': 64,
        'acquisition_campaign': 120,
        'acquisition_referrer': 120,
    }
    for name in sorted(expected):
        if name not in columns:
            continue
        dtype, length, nullable = columns[name]
        if dtype != 'character varying' or length != expected_lengths[name]:
            errors.append(
                f"{name} unexpected definition "
                f"(dtype={dtype!r}, length={length!r})"
            )
        if nullable != 'YES':
            errors.append(f"{name} should be nullable")
    cur.execute("""
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema='public' AND table_name='orders'
          AND column_name LIKE 'acquisition_%'
    """)
    if cur.fetchone()[0]:
        errors.append("acquisition columns exist in public.orders")
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
                print(f"ERROR: {error}")
            return 1
        print("OK: migration 0018 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())