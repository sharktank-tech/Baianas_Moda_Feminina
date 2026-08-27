#!/usr/bin/env python3
"""Smoke test PÓS-migration 0003 em PostgreSQL REAL (staging/dev), ISOLADO.

Valida o schema-alvo UUID criando um SCHEMA DESCARTÁVEL dedicado
(`pollo_smoke_<random>`) e rodando tudo lá dentro. O schema `public` (legado)
e o schema `pollomodas` (produção da PolloModas) NÃO são tocados: nenhuma
tabela de produção é dropada ou alterada por este script. Ao final (sucesso ou
erro) o schema isolado é removido com `DROP SCHEMA ... CASCADE`.

Baseline de IDs no fixture:
  VARCHAR(36) = estado legado PRÉ-0003 (drift)        -> NÃO usado aqui
  UUID        = baseline suportado PÓS-0003 (este script)

Roteiro:
  schema isolado (UUID) -> create_app() -> db.create_all() -> ensure_schema()
  -> ensure_schema() novamente (idempotência) -> confirma FKs -> confirma índices

O schema `public`/`pollomodas` só devem ser lidos/auditados e alterados por
migrations explícitas aprovadas; este script NÃO roda nenhuma migration neles.

Uso:
  DATABASE_URL=postgresql://user:pass@host:5432/db \
  SECRET_KEY=smoke-test \
  python scripts/smoke_test_postgres.py
"""
import os
import sys
import secrets
import sqlalchemy
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from sqlalchemy import create_engine, text, inspect as sa_inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY") or "smoke-test-secret"
SCHEMA = os.environ.get("SMOKE_SCHEMA") or ("pollo_smoke_" + secrets.token_hex(4))

if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
    print("ERRO: defina DATABASE_URL para um PostgreSQL (postgresql://...). "
          "Este script NÃO roda em SQLite e NÃO mexe nos schemas public/pollomodas.")
    sys.exit(2)

# Configura o ambiente ANTES de importar a aplicação, para que:
#   - config.SQLALCHEMY_ENGINE_OPTIONS injete options=-csearch_path=<SCHEMA>;
#   - loja.schema.DB_SCHEMA (usado por ensure_schema) aponte para o schema
#     descartável. Assim a app e o schema.py miram SOMENTE o schema isolado.
os.environ["POLLOMODAS_SCHEMA"] = SCHEMA
os.environ["SECRET_KEY"] = SECRET_KEY
os.environ["DATABASE_URL"] = DATABASE_URL

from loja.schema import (
    ensure_schema,
    assert_safe_disposable_schema,
    install_search_path_hook,
)

# Proteção: recusa nomes reservados (public/pollomodas/auth/storage/extensions)
# e exige prefixo pollo_smoke_, mesmo que SMOKE_SCHEMA venha mal configurado.
assert_safe_disposable_schema(SCHEMA)

from loja import db, create_app  # noqa: E402

# Engine bruto do smoke: aplica search_path via startup option na URL.
def _with_search_path(url, schema):
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["options"] = "-csearch_path=%s" % schema
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


ENGINE_URL = _with_search_path(DATABASE_URL, SCHEMA)


# Baseline PÓS-0003: IDs legados são UUID (não VARCHAR).
# users.id também é UUID no model; recriado aqui para fechar o drift users.id.
# shipments.id é BIGINT (model); o resto UUID.
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


def create_isolated_schema(engine):
    """Cria o schema descartável (limpo) onde todo o smoke roda."""
    # Proteção antes de qualquer DDL destrutivo.
    assert_safe_disposable_schema(SCHEMA)
    assert SCHEMA.startswith("pollo_smoke_")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA))
        conn.execute(text("CREATE SCHEMA %s" % SCHEMA))
        conn.commit()


def seed_legacy(engine, tables):
    """Recria apenas as tabelas legadas DENTRO do schema isolado."""
    assert_safe_disposable_schema(SCHEMA)
    with engine.connect() as conn:
        for t, cols in tables.items():
            conn.execute(text("DROP TABLE IF EXISTS %s CASCADE" % t))
            conn.execute(text("CREATE TABLE %s (%s)" % (t, cols)))
        conn.commit()


def cleanup(engine):
    """Remove o schema isolado. Nunca mexe em `public` nem `pollomodas`."""
    assert_safe_disposable_schema(SCHEMA)
    assert SCHEMA.startswith("pollo_smoke_")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA))
        conn.commit()


def get_fk(inspector, table, column, ref_table, ref_column):
    for fk in inspector.get_foreign_keys(table, schema=SCHEMA):
        if (fk.get("referred_table") == ref_table
                and column in (fk.get("constrained_columns") or [])
                and ref_column in (fk.get("referred_columns") or [])):
            return True
    return False


def get_index(inspector, table, column):
    for idx in inspector.get_indexes(table, schema=SCHEMA):
        if column in (idx.get("column_names") or []):
            return True
    return False


def validate(app, label):
    with app.app_context():
        inspector = sa_inspect(db.engine)
        existing = set(inspector.get_table_names(schema=SCHEMA))

        missing = NEW_TABLES - existing
        assert not missing, "tabelas novas ausentes: %s" % missing
        print("[%s] OK tabelas novas: %s" % (label, sorted(NEW_TABLES)))

        for table, (col, ref_t, ref_c) in FK_TARGETS.items():
            cols = {c["name"] for c in inspector.get_columns(table, schema=SCHEMA)}
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

        snapshot = {}
        for t in list(FK_TARGETS) + ["shipments", "products"]:
            if t in existing:
                snapshot[t] = {
                    "cols": {c["name"] for c in inspector.get_columns(t, schema=SCHEMA)},
                    "fks": [(fk.get("constrained_columns"), fk.get("referred_table"))
                            for fk in inspector.get_foreign_keys(t, schema=SCHEMA)],
                    "idx": [(ix.get("name"), tuple(ix.get("column_names") or []))
                            for ix in inspector.get_indexes(t, schema=SCHEMA)],
                }
        return snapshot


def main():
    print("== Smoke isolado no schema: %s (public/pollomodas NÃO são tocados) ==" % SCHEMA)
    engine = create_engine(ENGINE_URL)
    # Garante que o DDL do próprio smoke (seed_legacy usa nomes sem schema
    # qualificado) caia no schema isolado, mesmo se o pooler Supabase ignorar
    # options=-csearch_path. Mesmo mecanismo de create_app().
    install_search_path_hook(engine, SCHEMA)
    try:
        create_isolated_schema(engine)

        # ---- Cenário A: schema legado SEM novas colunas -------------------
        print("\n== Cenário A: legacy sem novas colunas (ADD COLUMN ... REFERENCES) ==")
        seed_legacy(engine, LEGACY_TABLES)
        app = create_app()  # create_all (novas) + ensure_schema
        snap_a = validate(app, "A")
        with app.app_context():
            ensure_schema(app)
        snap_a2 = validate(app, "A-rerun")
        assert snap_a == snap_a2, "estado mudou após rerun (não idempotente?)"
        print("[A] OK idempotência: estado idêntico após rerun de ensure_schema")

        # ---- Cenário B: colunas presentes, SEM FK (ALTER ADD CONSTRAINT) --
        print("\n== Cenário B: legacy com colunas, sem FK (ALTER ADD CONSTRAINT) ==")
        seed_legacy(engine, LEGACY_TABLES_B)
        app_b = create_app()
        snap_b = validate(app_b, "B")
        with app_b.app_context():
            ensure_schema(app_b)
        snap_b2 = validate(app_b, "B-rerun")
        assert snap_b == snap_b2, "estado B mudou após rerun"
        print("[B] OK FKs adicionadas via ALTER ADD CONSTRAINT + idempotente")

        print("\nSMOKE TEST POSTGRES: PASS")
    finally:
        cleanup(engine)
        print("schema isolado %s removido (public/pollomodas intactos)" % SCHEMA)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print("SMOKE TEST POSTGRES: FAIL -> %s" % exc)
        sys.exit(1)
    except sqlalchemy.exc.SQLAlchemyError as exc:
        print("SMOKE TEST POSTGRES: ERRO DE SQL -> %s" % exc)
        sys.exit(1)
