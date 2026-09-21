"""Valida 0023 em PostgreSQL descartável, duas vezes e fora de public."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from test_aliexpress_payment_guard_pg import DATABASE_URL, IS_POSTGRES, env  # noqa: F401

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL descartável",
)


def test_recovery_skip_migration_is_idempotent_and_scoped(env):
    schema, _ = env
    assert schema.startswith("pollo_test_")
    script = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "0023_checkout_recovery_skip.sql"
    ).read_text(encoding="utf-8")
    script = script.replace("pollomodas", schema)

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            before_public = connection.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            ).scalar_one()
            connection.commit()

            for _ in range(2):
                connection.exec_driver_sql(script)
                connection.commit()

            columns = connection.execute(
                text(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = :schema
                       AND table_name = 'orders'
                       AND column_name IN (
                           'recovery_email_skipped_at',
                           'recovery_email_skipped_by_user_id',
                           'recovery_email_skip_reason'
                       )
                    """
                ),
                {"schema": schema},
            ).scalars().all()
            assert set(columns) == {
                "recovery_email_skipped_at",
                "recovery_email_skipped_by_user_id",
                "recovery_email_skip_reason",
            }

            constraints = connection.execute(
                text(
                    """
                    SELECT conname, contype, confdeltype
                      FROM pg_constraint
                     WHERE conrelid = to_regclass(:orders)
                       AND conname IN (
                           'orders_recovery_email_skipped_by_user_id_fkey',
                           'ck_orders_recovery_email_skip_reason'
                       )
                    """
                ),
                {"orders": f"{schema}.orders"},
            ).all()
            by_name = {row.conname: row for row in constraints}
            assert by_name[
                "orders_recovery_email_skipped_by_user_id_fkey"
            ].contype == "f"
            assert by_name[
                "orders_recovery_email_skipped_by_user_id_fkey"
            ].confdeltype == "n"
            assert by_name["ck_orders_recovery_email_skip_reason"].contype == "c"

            index_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                      FROM pg_indexes
                     WHERE schemaname = :schema
                       AND tablename = 'orders'
                       AND indexname =
                           'ix_orders_recovery_email_skipped_by_user_id'
                    """
                ),
                {"schema": schema},
            ).scalar_one()
            assert index_count == 1

            after_public = connection.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            ).scalar_one()
            assert after_public == before_public
    finally:
        engine.dispose()
