"""Verify the trigger hardening migration in disposable PostgreSQL only."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from test_aliexpress_payment_guard_pg import DATABASE_URL, IS_POSTGRES, env  # noqa: F401

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requer PostgreSQL descartável")


def test_trigger_function_search_path_migration_is_idempotent(env):
    schema, _ = env
    assert schema.startswith("baianas_smoke_")
    script = (Path(__file__).resolve().parents[1] / "migrations" /
              "0022_canary_evidence_function_search_path.sql").read_text(encoding="utf-8")
    script = script.replace("baianas_moda_feminina", schema)
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            before = conn.execute(text(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            )).scalar_one()
            conn.commit()
            for _ in range(2):
                conn.exec_driver_sql(script)
                conn.commit()
            result = conn.execute(text("""
                SELECT p.proconfig, t.tgenabled
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                JOIN pg_trigger t ON t.tgfoid = p.oid
                JOIN pg_class c ON c.oid = t.tgrelid
                WHERE n.nspname = :schema
                  AND p.proname = 'canary_evidence_reject_mutation'
                  AND c.relname = 'canary_evidence_events'
                  AND t.tgname = 'canary_evidence_immutable'
            """), {"schema": schema}).one()
            assert f"search_path=pg_catalog, {schema}" in result.proconfig
            assert result.tgenabled == "O"
            after = conn.execute(text(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            )).scalar_one()
            assert after == before
    finally:
        engine.dispose()
