"""Validator da migration 0019: lembrete de recuperação de checkout (FASE 9.0.0)."""
from __future__ import annotations
import os


def validate_schema(conn):
    errors = []
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema='pollomodas' AND table_name='orders'
          AND column_name = 'recovery_email_sent_at'
    """)
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    if 'recovery_email_sent_at' not in columns:
        errors.append("column recovery_email_sent_at NOT FOUND")
    else:
        dtype, length, nullable = columns['recovery_email_sent_at']
        if dtype != 'timestamp without time zone':
            errors.append(
                f"recovery_email_sent_at unexpected dtype {dtype!r}"
            )
        if nullable != 'YES':
            errors.append("recovery_email_sent_at should be nullable")
    cur.execute("""
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema='public' AND table_name='orders'
          AND column_name LIKE 'recovery_email_%'
    """)
    if cur.fetchone()[0]:
        errors.append("recovery_email columns exist in public.orders")
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
        print("OK: migration 0019 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())