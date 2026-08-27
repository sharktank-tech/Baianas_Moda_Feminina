#!/usr/bin/env python3
"""Smoke test PÓS-migration 0003 em PostgreSQL REAL (staging/dev).

Valida o schema-alvo UUID:
  schema UUID (baseline pós-0003)
  -> create_app()
  -> db.create_all()             (cria tabelas novas; product_supplier via FK UUID)
  -> ensure_schema()             (adiciona colunas/FKs/índices faltantes)
  -> ensure_schema() novamente   (idempotência)
  -> confirma FKs
  -> confirma índices

Baseline de IDs no fixture:
  VARCHAR(36) = estado legado PRÉ-0003 (drift)        -> NÃO usado aqui
  UUID        = baseline suportado PÓS-0003 (este script)

Dois cenários (ambos com baseline UUID):
  A) schema legado SEM as novas colunas  -> exercita ADD COLUMN ... REFERENCES
  B) schema legado COM as colunas, SEM FK -> exercita ALTER ADD CONSTRAINT
     (este é o ramo PostgreSQL que não é coberto por SQLite)

ATENÇÃO: aponte DATABASE_URL para um banco PostgreSQL DESCARTÁVEL de
teste/staging. O script recria apenas as tabelas legadas listadas em
LEGACY_TABLES (DROP ... CASCADE). Não altera models nem product_supplier.

Uso:
  DATABASE_URL=postgresql://user:pass@host:5432/pollo_smoketest \
  SECRET_KEY=smoke-test \
  python scripts/smoke_test_postgres.py
"""
import os
import sys

# Garante que o pacote 'loja' (raiz do repo) esteja importável quando o
# script é executado diretamente (python scripts/smoke_test_postgres.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy
from sqlalchemy import create_engine, text, inspect as sa_inspect

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY") or "smoke-test-secret"

if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
    print("ERRO: defina DATABASE_URL para um PostgreSQL de teste "
          "(postgresql://...). Este script NÃO roda em SQLite.")
    sys.exit(2)

# Garante que o app leia este DATABASE_URL/SECRET_KEY ao importar config.
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["SECRET_KEY"] = SECRET_KEY

# Importa o app APÓS fixar env.
from loja import db, create_app  # noqa: E402
from loja.schema import ensure_schema  # noqa: E402


# Baseline PÓS-0003: IDs legados são UUID (não VARCHAR).
#   VARCHAR(36) = estado legado PRÉ-0003 (drift)  -- não usado aqui
#   UUID        = baseline suportado PÓS-0003
# users.id também é UUID no model; o fixture recria users com UUID para
# fechar o drift users.id (BIGINT no legado real) detectado no smoke.
# Tabelas NOVAS ficam ausentes de propósito (criadas por db.create_all).
LEGACY_TABLES = {
    "users": "id UUID PRIMARY KEY",
    "orders": "id UUID PRIMARY KEY, order_number TEXT",
    "suppliers": "id UUID PRIMARY KEY, name TEXT",
    "products": "id UUID PRIMARY KEY, name TEXT, price TEXT, "
                "stock INTEGER, category TEXT",
    "cart_items": "id UUID PRIMARY KEY, cart_id UUID, "
                  "product_id UUID, selected_size TEXT, "
                  "quantity INTEGER, unit_price TEXT",
    "order_items": "id UUID PRIMARY KEY, order_id UUID, "
                   "product_id UUID, product_name TEXT, "
                   "quantity INTEGER, unit_price TEXT, line_total TEXT",
    "shipments": "id BIGINT PRIMARY KEY, order_id UUID, "
                 "tracking_code TEXT",
}

# Cenário B: colunas novas JÁ presentes, mas SEM as FKs (exercita o ramo
# PostgreSQL de ALTER ADD CONSTRAINT em coluna existente). Baseline UUID.
LEGACY_TABLES_B = dict(LEGACY_TABLES)
LEGACY_TABLES_B["products"] = (
    "id UUID PRIMARY KEY, name TEXT, price TEXT, stock INTEGER, "
    "category TEXT, category_id UUID"
)
LEGACY_TABLES_B["cart_items"] = (
    "id UUID PRIMARY KEY, cart_id UUID, product_id UUID, "
    "selected_size TEXT, quantity INTEGER, unit_price TEXT, variant_id UUID"
)
LEGACY_TABLES_B["order_items"] = (
    "id UUID PRIMARY KEY, order_id UUID, product_id UUID, "
    "product_name TEXT, quantity INTEGER, unit_price TEXT, line_total TEXT, "
    "variant_id UUID"
)

NEW_TABLES = {
    "categories", "product_images", "product_variants", "supplier_products",
    "supplier_category_mappings", "product_syncs", "aliexpress_orders",
    "sync_logs",
}

FK_TARGETS = {
    "products": ("category_id", "categories", "id"),
    "cart_items": ("variant_id", "product_variants", "id"),
    "order_items": ("variant_id", "product_variants", "id"),
}
INDEX_TARGETS = [
    ("products", "category_id"),
    ("cart_items", "variant_id"),
    ("order_items", "variant_id"),
    ("shipments", "ali_order_id"),
    ("shipments", "external_shipment_id"),
]


def setup_legacy(engine, tables):
    with engine.connect() as conn:
        for t in tables:
            conn.execute(text("DROP TABLE IF EXISTS %s CASCADE" % t))
        conn.commit()
        for t, cols in tables.items():
            conn.execute(text("CREATE TABLE %s (%s)" % (t, cols)))
        conn.commit()


def get_fk(inspector, table, column, ref_table, ref_column):
    for fk in inspector.get_foreign_keys(table):
        if (fk.get("referred_table") == ref_table
                and column in (fk.get("constrained_columns") or [])
                and ref_column in (fk.get("referred_columns") or [])):
            return True
    return False


def get_index(inspector, table, column):
    for idx in inspector.get_indexes(table):
        if column in (idx.get("column_names") or []):
            return True
    return False


def validate(app, label):
    with app.app_context():
        inspector = sa_inspect(db.engine)
        existing = set(inspector.get_table_names())

        missing = NEW_TABLES - existing
        assert not missing, "tabelas novas ausentes: %s" % missing
        print("[%s] OK tabelas novas: %s" % (label, sorted(NEW_TABLES)))

        for table, (col, ref_t, ref_c) in FK_TARGETS.items():
            cols = {c["name"] for c in inspector.get_columns(table)}
            assert col in cols, "%s.%s ausente" % (table, col)
            assert get_fk(inspector, table, col, ref_t, ref_c), \
                "FK %s.%s -> %s.%s ausente" % (table, col, ref_t, ref_c)
            print("[%s] OK %s.%s + FK -> %s.%s" % (label, table, col, ref_t, ref_c))

        for table, col in INDEX_TARGETS:
            if table not in existing:
                continue
            assert get_index(inspector, table, col), \
                "índice %s.%s ausente" % (table, col)
            print("[%s] OK índice %s.%s" % (label, table, col))

        # captura estado para checagem de idempotência
        snapshot = {}
        for t in list(FK_TARGETS) + ["shipments", "products"]:
            if t in existing:
                snapshot[t] = {
                    "cols": {c["name"] for c in inspector.get_columns(t)},
                    "fks": [(fk.get("constrained_columns"), fk.get("referred_table"))
                            for fk in inspector.get_foreign_keys(t)],
                    "idx": [(ix.get("name"), tuple(ix.get("column_names") or []))
                            for ix in inspector.get_indexes(t)],
                }
        return snapshot


def main():
    engine = create_engine(DATABASE_URL)

    # ---- Cenário A: schema legado SEM novas colunas -----------------------
    print("== Cenário A: legacy sem novas colunas (ADD COLUMN ... REFERENCES) ==")
    setup_legacy(engine, LEGACY_TABLES)
    app = create_app()  # create_all (novas) + ensure_schema
    snap_a = validate(app, "A")

    # reiniciar app: rodar ensure_schema de novo (idempotência)
    with app.app_context():
        ensure_schema(app)
    snap_a2 = validate(app, "A-rerun")
    assert snap_a == snap_a2, "estado mudou após rerun (não idempotente?)"
    print("[A] OK idempotência: estado idêntico após rerun de ensure_schema")

    # ---- Cenário B: colunas presentes, SEM FK (ALTER ADD CONSTRAINT) --------
    print("\n== Cenário B: legacy com colunas, sem FK (ALTER ADD CONSTRAINT) ==")
    # recria apenas as tabelas legadas em estado B; tabelas novas permanecem.
    setup_legacy(engine, LEGACY_TABLES_B)
    # novo boot: create_all (novas, já existem -> skip) + ensure_schema
    # (agora os ADD CONSTRAINT em coluna existente são exercitados).
    app_b = create_app()
    snap_b = validate(app_b, "B")
    with app_b.app_context():
        ensure_schema(app_b)
    snap_b2 = validate(app_b, "B-rerun")
    assert snap_b == snap_b2, "estado B mudou após rerun"
    print("[B] OK FKs adicionadas via ALTER ADD CONSTRAINT + idempotente")

    print("\nSMOKE TEST POSTGRES: PASS")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print("SMOKE TEST POSTGRES: FAIL -> %s" % exc)
        sys.exit(1)
    except sqlalchemy.exc.SQLAlchemyError as exc:
        print("SMOKE TEST POSTGRES: ERRO DE SQL -> %s" % exc)
        sys.exit(1)
