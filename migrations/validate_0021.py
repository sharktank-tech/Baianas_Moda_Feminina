"""Validator da migration 0021 — schema baianas_moda_feminina privado."""

from __future__ import annotations
import os


ROLES = ("anon", "authenticated", "service_role", "authenticator")


def validate_schema(conn):
    errors = []
    cur = conn.cursor()

    for role in ROLES:
        cur.execute(
            "SELECT has_schema_privilege(%s, 'baianas_moda_feminina', 'USAGE'), "
            "has_schema_privilege(%s, 'baianas_moda_feminina', 'CREATE')",
            (role, role),
        )
        usage, create = cur.fetchone()
        if usage or create:
            errors.append(f"{role} has schema privileges")

        cur.execute("""
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='baianas_moda_feminina'
              AND c.relkind IN ('r', 'v', 'm')
              AND (
                has_table_privilege(%s, c.oid, 'SELECT')
                OR has_table_privilege(%s, c.oid, 'INSERT')
                OR has_table_privilege(%s, c.oid, 'UPDATE')
                OR has_table_privilege(%s, c.oid, 'DELETE')
            )
        """, (role, role, role, role))
        if cur.fetchone()[0]:
            errors.append(f"{role} has relation privileges")

        cur.execute("""
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='baianas_moda_feminina'
              AND c.relkind='S'
              AND (
                has_sequence_privilege(%s, c.oid, 'USAGE')
                OR has_sequence_privilege(%s, c.oid, 'SELECT')
                OR has_sequence_privilege(%s, c.oid, 'UPDATE')
              )
        """, (role, role, role))
        if cur.fetchone()[0]:
            errors.append(f"{role} has sequence privileges")

        cur.execute("""
            SELECT count(*)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='baianas_moda_feminina'
              AND has_function_privilege(%s, p.oid, 'EXECUTE')
        """, (role,))
        if cur.fetchone()[0]:
            errors.append(f"{role} has function privileges")

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
        print("OK: baianas_moda_feminina schema is private")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
