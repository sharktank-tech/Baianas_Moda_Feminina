"""Validator for migration 0014: nullable customer CPF on baianas_moda_feminina.orders."""

from __future__ import annotations

import os
import sys


def _check_pg_available() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    return bool(url and url.startswith("postgresql"))


def validate_schema(conn) -> list[str]:
    errors: list[str] = []
    cur = conn.cursor()

    cur.execute("""
        SELECT data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'baianas_moda_feminina'
          AND table_name = 'orders'
          AND column_name = 'customer_cpf'
    """)
    row = cur.fetchone()
    if row is None:
        errors.append("column customer_cpf NOT FOUND")
        return errors

    data_type, max_length, is_nullable = row
    if data_type.lower() != "character varying":
        errors.append(f"customer_cpf type unexpected: {data_type!r}")
    if int(max_length or 0) != 11:
        errors.append(f"customer_cpf length expected 11, got {max_length!r}")
    if is_nullable.upper() != "YES":
        errors.append("customer_cpf must remain nullable for legacy orders")

    cur.execute("""
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conname = 'ck_orders_customer_cpf_length'
          AND conrelid = 'baianas_moda_feminina.orders'::regclass
    """)
    ck = cur.fetchone()
    if ck is None:
        errors.append("CHECK ck_orders_customer_cpf_length NOT FOUND")
    else:
        definition = ck[0].lower()
        if "customer_cpf" not in definition or "length" not in definition:
            errors.append("CPF CHECK definition is incomplete")
        if "is null" not in definition:
            errors.append("CPF CHECK must allow NULL for legacy orders")

    cur.execute("""
        SELECT count(*)
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'orders'
          AND column_name = 'customer_cpf'
    """)
    if cur.fetchone()[0] != 0:
        errors.append("customer_cpf unexpectedly exists in public.orders")

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
            for error in errors:
                print(f"FAIL: {error}")
            return 1
        print("OK: migration 0014 validated")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
