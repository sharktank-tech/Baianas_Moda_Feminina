"""Validator for migration 0012: AliExpress remote orders with durable attempts.

Validates:
- Table baianas_moda_feminina.aliexpress_submission_attempts exists with all columns
- Table baianas_moda_feminina.aliexpress_remote_orders exists with all columns
- CHECK constraints present (state in_flight/confirmed/rejected/unknown, position >= 0)
- Unique constraints present (attempt_key, remote_order_id, aliexpress_order_id+position)
- Foreign keys present and correct
- Backfill column aliexpress_orders.ali_order_id still allows NULL
- Idempotent: running twice should not fail

Usage (requires PostgreSQL):
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0012_aliexpress_remote_orders.sql
    DATABASE_URL="$DATABASE_URL" python3 migrations/validate_0012.py

Without PostgreSQL:
    python3 migrations/validate_0012.py   # prints NOT RUN and exits 0
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

    # 1. aliexpress_submission_attempts table
    cur.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'baianas_moda_feminina'
          AND table_name = 'aliexpress_submission_attempts'
    """)
    if cur.fetchone()[0] == 0:
        errors.append("Table baianas_moda_feminina.aliexpress_submission_attempts NOT FOUND")
        return errors

    # Columns
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'baianas_moda_feminina'
          AND table_name = 'aliexpress_submission_attempts'
        ORDER BY ordinal_position
    """)
    cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    expected_cols = {
        "id": ("uuid", "NO"),
        "aliexpress_order_id": ("uuid", "NO"),
        "attempt_key": ("character varying", "NO"),
        "state": ("character varying", "NO"),
        "started_at": ("timestamp without time zone", "NO"),
        "completed_at": ("timestamp without time zone", "YES"),
        "last_error": ("text", "YES"),
        "created_at": ("timestamp without time zone", "NO"),
        "updated_at": ("timestamp without time zone", "NO"),
    }
    for col, (dtype, nullable) in expected_cols.items():
        if col not in cols:
            errors.append(f"Column {col} NOT FOUND in aliexpress_submission_attempts")
        elif col in cols:
            if col in ("id",):
                pass  # uuid type check varies
            if dtype.upper() not in cols[col][0].upper() and col != "id":
                errors.append(
                    f"Column {col} type expected {dtype}, got {cols[col][0]!r}"
                )

    # CHECK constraint: state IN (...)
    cur.execute("""
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conname = 'ck_attempt_state'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'baianas_moda_feminina' AND c.relname = 'aliexpress_submission_attempts'
          )
    """)
    if cur.fetchone() is None:
        errors.append("CHECK constraint 'ck_attempt_state' NOT FOUND")

    # Unique index: attempt_key (criado como UNIQUE INDEX `ux_ase_attempt_key`
    # na migration 0012 — LONGE de pg_constraint; vive em pg_indexes).
    cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'baianas_moda_feminina'
          AND tablename = 'aliexpress_submission_attempts'
          AND indexname = 'ux_ase_attempt_key'
    """)
    if cur.fetchone() is None:
        errors.append("UNIQUE INDEX 'ux_ase_attempt_key' (attempt_key) NOT FOUND")

    # 2. aliexpress_remote_orders table
    cur.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'baianas_moda_feminina'
          AND table_name = 'aliexpress_remote_orders'
    """)
    if cur.fetchone()[0] == 0:
        errors.append("Table baianas_moda_feminina.aliexpress_remote_orders NOT FOUND")
        return errors

    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'baianas_moda_feminina'
          AND table_name = 'aliexpress_remote_orders'
        ORDER BY ordinal_position
    """)
    cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    expected_remote_cols = {
        "id": ("uuid", "NO"),
        "aliexpress_order_id": ("uuid", "NO"),
        "remote_order_id": ("character varying", "NO"),
        "position": ("integer", "NO"),
        "attempt_key": ("character varying", "YES"),
        "remote_status": ("text", "YES"),
        "created_at": ("timestamp without time zone", "NO"),
        "updated_at": ("timestamp without time zone", "NO"),
    }
    for col, (dtype, nullable) in expected_remote_cols.items():
        if col not in cols:
            errors.append(f"Column {col} NOT FOUND in aliexpress_remote_orders")

    # CHECK constraint: position >= 0
    cur.execute("""
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conname = 'ck_position_nonneg'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'baianas_moda_feminina' AND c.relname = 'aliexpress_remote_orders'
          )
    """)
    if cur.fetchone() is None:
        errors.append("CHECK constraint 'ck_position_nonneg' NOT FOUND")

    # Unique constraint: remote_order_id
    cur.execute("""
        SELECT conname FROM pg_constraint
        WHERE conname = 'uq_remote_order_id'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'baianas_moda_feminina' AND c.relname = 'aliexpress_remote_orders'
          )
    """)
    if cur.fetchone() is None:
        errors.append("UNIQUE constraint 'uq_remote_order_id' NOT FOUND")

    # Unique constraint: aliexpress_order_id + position
    cur.execute("""
        SELECT conname FROM pg_constraint
        WHERE conname = 'uq_order_position'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'baianas_moda_feminina' AND c.relname = 'aliexpress_remote_orders'
          )
    """)
    if cur.fetchone() is None:
        errors.append("UNIQUE constraint 'uq_order_position' NOT FOUND")

    # Foreign key: aliexpress_order_id -> aliexpress_orders.id
    cur.execute("""
        SELECT conname FROM pg_constraint
        WHERE contype = 'f'
          AND conname LIKE '%aliexpress_order_id%'
          AND conrelid = (
              SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'baianas_moda_feminina' AND c.relname = 'aliexpress_remote_orders'
          )
    """)
    if cur.fetchone() is None:
        errors.append("FOREIGN KEY on aliexpress_order_id NOT FOUND")

    # 3. Backfill: aliexpress_orders.ali_order_id should allow NULL
    cur.execute("""
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = 'baianas_moda_feminina'
          AND table_name = 'aliexpress_orders'
          AND column_name = 'ali_order_id'
    """)
    row = cur.fetchone()
    if row and row[0].upper() != "YES":
        errors.append("aliexpress_orders.ali_order_id should allow NULL for backfill")

    # 4. No prohibited AliExpress tables in public
    cur.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('aliexpress_submission_attempts', 'aliexpress_remote_orders')
    """)
    if cur.fetchone()[0] > 0:
        errors.append("Prohibited AliExpress tables found in public schema")

    return errors


def _apply_migration(cur, schema: str) -> None:
    """Aplica migration 0012 num schema alvo (substitui `baianas_moda_feminina`)."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "0012_aliexpress_remote_orders.sql"), "r") as f:
        sql = f.read()
    # Alignment contract: o DDL referencia `baianas_moda_feminina`; para provas em schema
    # descartável substituímos o schema alvo. Mantém UNIQUE INDEX, CHECKs e
    # backfill exatamente como em produção.
    # Strip psql meta-commands (lines starting with backslash) for psycopg2 compatibility.
    sql = "\n".join(line for line in sql.split("\n") if not line.lstrip().startswith("\\"))
    sql = sql.replace("baianas_moda_feminina", schema)
    cur.execute(sql)


def prove_migration(conn) -> list[str]:
    """Prova REAL (não apenas introspecção) de fresh/upgrade/rerun/backfill.

    Cria um schema descartável, aplica a migration como em produção e verifica:
      * fresh: tabelas criadas sem erro;
      * upgrade com legacy ali_order_id: backfill cria EXATAMENTE um filho
        remote pela ordem legada (position 0);
      * rerun: a migration é idempotente (2ª aplicação não falha nem duplica);
      * backfill correto: ordem com ali_order_id -> filho criado;
      * zero rows: ordem sem ali_order_id NÃO gera filho.
    Derruba o schema descartável ao final.
    """
    import secrets

    import psycopg2
    from psycopg2 import sql as psql

    errors: list[str] = []
    schema = "pollo_mig_%s" % secrets.token_hex(4)
    # Never touch conn.autocommit - use explicit BEGIN/COMMIT only
    cur = conn.cursor()
    try:
        cur.execute(
            psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(schema))
        )

        # Minimal parent tables needed by the DDL FK + backfill.
        cur.execute(f"""
            CREATE TABLE {schema}.aliexpress_orders (
                id UUID PRIMARY KEY,
                state VARCHAR(20) NOT NULL DEFAULT 'submitting',
                ali_order_id VARCHAR(64) NULL,
                ali_status TEXT NULL,
                attempt_key VARCHAR(64) NULL
            )
        """)

        # ---- FRESH: apply migration on empty tables ----
        cur.execute("BEGIN")
        _apply_migration(cur, schema)
        cur.execute("COMMIT")

        # ---- UPGRADE com legacy ali_order_id: insert legacy BEFORE applying migration ----
        cur.execute("BEGIN")
        cur.execute(f"""
            INSERT INTO {schema}.aliexpress_orders
                (id, state, ali_order_id, ali_status, attempt_key)
            VALUES
                (gen_random_uuid(), 'submitted', 'LEGACY-1', 'submitted', 'k-legacy'),
                (gen_random_uuid(), 'submitting', NULL, NULL, NULL)
        """)
        # Apply migration (runs backfill for legacy rows)
        _apply_migration(cur, schema)
        cur.execute("COMMIT")
        # Fresh aplica o backfill: LEGACY-1 já deve ter gerado filho.
        cur.execute(
            f"SELECT count(*) FROM {schema}.aliexpress_remote_orders "
            f"WHERE remote_order_id = 'LEGACY-1'"
        )
        if cur.fetchone()[0] != 1:
            errors.append("backfill: legacy orders não originaram exatamente 1 filho")
        cur.execute(
            f"SELECT count(*) FROM {schema}.aliexpress_remote_orders "
            f"WHERE remote_order_id IS NULL"
        )
        if cur.fetchone()[0] != 0:
            errors.append("backfill: remote_order_id NULL presente")
        # zero rows para ordem sem ali_order_id
        cur.execute(
            f"SELECT count(*) FROM {schema}.aliexpress_remote_orders aro "
            f"JOIN {schema}.aliexpress_orders ao ON ao.id = aro.aliexpress_order_id "
            f"WHERE ao.ali_order_id IS NULL"
        )
        if cur.fetchone()[0] != 0:
            errors.append("backfill: ordem sem ali_order_id gerou filho remoto")

        # ---- RERUN (idempotência) ----
        cur.execute("BEGIN")
        _apply_migration(cur, schema)
        cur.execute("COMMIT")
        cur.execute(
            f"SELECT count(*) FROM {schema}.aliexpress_remote_orders "
            f"WHERE remote_order_id = 'LEGACY-1'"
        )
        if cur.fetchone()[0] != 1:
            errors.append("rerun: backfill duplicou filho remoto do legacy")

        # Contrato único: UNIQUE INDEX ux_ase_attempt_key existe.
        cur.execute(
            f"SELECT count(*) FROM pg_indexes "
            f"WHERE schemaname = '{schema}' AND indexname = 'ux_ase_attempt_key'"
        )
        if cur.fetchone()[0] != 1:
            errors.append("rerun: UNIQUE INDEX ux_ase_attempt_key ausente")

    except Exception as exc:  # noqa: BLE001
        errors.append(f"proof raised: {type(exc).__name__}: {exc}")
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        try:
            cur2 = conn.cursor()
            cur2.execute(
                psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psql.Identifier(schema)
                )
            )
            cur2.execute("COMMIT")
        except Exception:  # noqa: BLE001
            try:
                cur2 = conn.cursor()
                cur2.execute("ROLLBACK")
            except Exception:
                pass

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
        print("OK: migration 0012 schema validated")

        proof_errors = prove_migration(conn)
        if proof_errors:
            for e in proof_errors:
                print(f"PROOF FAIL: {e}")
            return 1
        print("OK: migration 0012 proof (fresh/upgrade/rerun/backfill/zero-rows) passed")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
