"""Valida 0028 em PostgreSQL descartável: up idempotente, preservação dos
registros e persistência de rótulos de 21–40 caracteres.

Só roda quando DATABASE_URL (ORIGINAL, antes do conftest trocar por SQLite)
aponta para PostgreSQL — mesmo padrão dos demais test_*_pg.py. Usa schema
descartável próprio; NUNCA toca `pollomodas` nem produção.
"""

import secrets
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from test_aliexpress_payment_guard_pg import (  # noqa: F401
    DATABASE_URL,
    IS_POSTGRES,
)

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL (DATABASE_URL postgresql://)",
)

_MIG = Path(__file__).resolve().parents[1] / "migrations"


def _script(nome: str, schema: str) -> str:
    return (_MIG / nome).read_text(encoding="utf-8").replace(
        "pollomodas", schema
    )


def _largura(conn, schema, tabela, coluna):
    return conn.execute(
        text(
            """
            SELECT character_maximum_length FROM information_schema.columns
             WHERE table_schema = :s AND table_name = :t
               AND column_name = :c
            """
        ),
        {"s": schema, "t": tabela, "c": coluna},
    ).scalar_one()


def test_0028_up_idempotente_preserva_e_persiste_21_40():
    schema = "pollo_test_variant_size_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE SCHEMA %s" % schema))
            # Tabelas mínimas com os limites ANTIGOS.
            conn.execute(
                text(
                    "CREATE TABLE %s.cart_items "
                    "(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "selected_size varchar(10))" % schema
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE %s.order_items "
                    "(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "selected_size varchar(20))" % schema
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE %s.product_variants "
                    "(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "size varchar(20))" % schema
                )
            )
            # Semente com valores que cabem nos limites antigos.
            conn.execute(
                text(
                    "INSERT INTO %s.cart_items (selected_size) "
                    "VALUES ('M'), ('UNICO')" % schema
                )
            )
            conn.execute(
                text(
                    "INSERT INTO %s.order_items (selected_size) "
                    "VALUES ('5T(Size 120)')" % schema
                )
            )
            conn.execute(
                text(
                    "INSERT INTO %s.product_variants (size) "
                    "VALUES ('11-12T(Size 160)')" % schema
                )
            )
            conn.commit()

            up = _script("0028_variant_selected_size_labels.sql", schema)
            # UP duas vezes → idempotente.
            for _ in range(2):
                conn.exec_driver_sql(up)
                conn.commit()

            assert _largura(conn, schema, "cart_items", "selected_size") == 40
            assert _largura(conn, schema, "order_items", "selected_size") == 40
            assert _largura(conn, schema, "product_variants", "size") == 40

            # Registros preservados integralmente.
            assert (
                conn.execute(
                    text(
                        "SELECT selected_size FROM %s.cart_items "
                        "ORDER BY selected_size" % schema
                    )
                ).scalars()
                .all()
                == ["M", "UNICO"]
            )
            assert (
                conn.execute(
                    text(
                        "SELECT selected_size FROM %s.order_items" % schema
                    )
                ).scalar_one()
                == "5T(Size 120)"
            )
            assert (
                conn.execute(
                    text("SELECT size FROM %s.product_variants" % schema)
                ).scalar_one()
                == "11-12T(Size 160)"
            )

            # Persistência de rótulos de 21–40 caracteres (exatos, sem corte).
            rotulo21 = "T" * 21
            rotulo40 = "S" * 40
            conn.execute(
                text(
                    "INSERT INTO %s.cart_items (selected_size) "
                    "VALUES (:v)" % schema
                ),
                {"v": rotulo21},
            )
            conn.execute(
                text(
                    "INSERT INTO %s.order_items (selected_size) "
                    "VALUES (:v)" % schema
                ),
                {"v": rotulo40},
            )
            conn.execute(
                text("INSERT INTO %s.product_variants (size) VALUES (:v)" % schema),
                {"v": rotulo40},
            )
            conn.commit()
            assert (
                conn.execute(
                    text(
                        "SELECT selected_size FROM %s.cart_items "
                        "WHERE selected_size = :v" % schema
                    ),
                    {"v": rotulo21},
                ).scalar_one()
                == rotulo21
            )
            assert (
                conn.execute(
                    text(
                        "SELECT selected_size FROM %s.order_items "
                        "WHERE selected_size = :v" % schema
                    ),
                    {"v": rotulo40},
                ).scalar_one()
                == rotulo40
            )
            assert (
                conn.execute(
                    text(
                        "SELECT size FROM %s.product_variants "
                        "WHERE size = :v" % schema
                    ),
                    {"v": rotulo40},
                ).scalar_one()
                == rotulo40
            )
    finally:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % schema))
            conn.commit()
