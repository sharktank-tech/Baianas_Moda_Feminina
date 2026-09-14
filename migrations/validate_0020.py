"""Validator da migration 0020 — métricas agregadas de aquisição."""

from __future__ import annotations
import os


def validate_schema(conn):
    errors = []
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema='pollomodas'
          AND table_name='acquisition_daily_metrics'
    """)
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    expected = {"id", "day", "channel", "sessions"}
    if set(columns) != expected:
        errors.append(f"unexpected columns: {sorted(columns)}")
    if columns.get("sessions") != ("integer", "NO"):
        errors.append("sessions must be non-null integer")
    cur.execute("""
        SELECT to_regclass('public.acquisition_daily_metrics')
    """)
    if cur.fetchone()[0] is not None:
        errors.append("table exists in public")
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
        print("OK: migration 0020 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
