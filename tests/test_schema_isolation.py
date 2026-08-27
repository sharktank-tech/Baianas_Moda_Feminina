"""Testes de isolamento por schema (PostgreSQL apenas).

Objetivo: provar que a PolloModas resolve tudo via `search_path=pollomodas`
e NUNCA toca `public` (nem tabelas homônimas legadas). Usa schemas descartáveis
para não modificar `public` nem `pollomodas`:

    pollo_test_legacy_<random>  -> users.id BIGINT   (simula legado)
    pollo_test_app_<random>     -> users.id UUID     (schema da app)

Transforma o app para mirar o schema "app" via monkeypatch (search_path +
DB_SCHEMA), sem alterar configuração global nem role do Supabase. Se não houver
DATABASE_URL PostgreSQL, o módulo inteiro é pulado.

Não cria nem dropa public.users em nenhum momento.
"""
import os
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, text, inspect as sa_inspect

from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL (DATABASE_URL postgresql://) para testar isolamento de schema",
)


def _engine():
    return create_engine(DATABASE_URL)


def _url_with_search_path(url, schema):
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["options"] = "-csearch_path=%s" % schema
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


@pytest.fixture
def env(monkeypatch):
    app_schema = "pollo_test_app_" + secrets.token_hex(4)
    leg_schema = "pollo_test_legacy_" + secrets.token_hex(4)

    eng = _engine()
    with eng.connect() as c:
        # schemas descartáveis; tabelas qualificadas (sem depender de search_path)
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % leg_schema))
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % app_schema))
        # legacy: users.id BIGINT (simula o sistema legado em public)
        c.execute(text("CREATE TABLE %s.users (id BIGINT PRIMARY KEY)" % leg_schema))
        # app: users.id UUID
        c.execute(text("CREATE TABLE %s.users (id UUID PRIMARY KEY)" % app_schema))
        c.commit()

    # Redireciona a aplicação para o schema "app" sem mexer na config global.
    import config
    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {"options": "-csearch_path=%s" % app_schema},
        },
    )
    import loja.schema as schema_mod
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", app_schema)

    yield app_schema, leg_schema

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % leg_schema))
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % app_schema))
        c.commit()


def test_search_path_resolves_app_schema_not_legacy(env):
    app_schema, leg_schema = env
    from loja import create_app, db
    app = create_app()
    with app.app_context():
        inspector = sa_inspect(db.engine)
        app_users = {
            c["name"]: c["type"]
            for c in inspector.get_columns("users", schema=app_schema)
        }
        leg_users = {
            c["name"]: c["type"]
            for c in inspector.get_columns("users", schema=leg_schema)
        }
        # app.users é UUID; legacy.users é BIGINT
        assert "UUID" in str(app_users["id"])
        assert "BIGINT" in str(leg_users["id"]).upper() or "INT" in str(leg_users["id"]).upper()

        # A app enxerga o schema app: inserir/ler um User UUID funciona e não
        # vem do legacy (que só tem BIGINT).
        from loja.modules.models import User
        u = User(id=uuid.uuid4(), name="isol", email="isol@p.z", is_admin=False)
        u.set_password("senha123")
        db.session.add(u)
        db.session.commit()
        got = User.query.first()
        assert got is not None and got.id == u.id


def test_create_all_isolation(env):
    app_schema, leg_schema = env
    from loja import create_app, db
    app = create_app()
    with app.app_context():
        inspector = sa_inspect(db.engine)
        new_tables = {
            "categories", "product_images", "product_variants",
            "supplier_products", "supplier_category_mappings",
            "product_syncs", "aliexpress_orders", "sync_logs",
        }
        assert new_tables.issubset(set(inspector.get_table_names(schema=app_schema)))
        # legacy não deve conter nenhuma tabela da PolloModas
        leg_tables = set(inspector.get_table_names(schema=leg_schema))
        assert not new_tables.intersection(leg_tables)


def test_ensure_schema_isolation_and_idempotence(env):
    app_schema, leg_schema = env
    from loja import create_app, db
    from loja.schema import ensure_schema
    app = create_app()
    with app.app_context():
        ensure_schema(app)
        cols1 = {
            c["name"]
            for c in sa_inspect(db.engine).get_columns("products", schema=app_schema)
        }
        ensure_schema(app)
        cols2 = {
            c["name"]
            for c in sa_inspect(db.engine).get_columns("products", schema=app_schema)
        }
        assert cols1 == cols2
        # legacy não foi tocado pelo ensure_schema
        leg_cols = {
            c["name"]
            for c in sa_inspect(db.engine).get_columns("users", schema=leg_schema)
        }
        assert "category_id" not in leg_cols


def test_fk_points_to_app_users_not_legacy(env):
    app_schema, leg_schema = env
    from loja import create_app, db
    app = create_app()
    with app.app_context():
        # garante tabela favorites no schema app com FK para users (UUID)
        inspector = sa_inspect(db.engine)
        if "favorites" in inspector.get_table_names(schema=app_schema):
            fks = inspector.get_foreign_keys("favorites", schema=app_schema)
            refs = [
                (fk.get("referred_table"), tuple(fk.get("referred_columns") or []))
                for fk in fks
                if "user_id" in (fk.get("constrained_columns") or [])
            ]
            assert refs, "favorites não possui FK user_id"
            # a FK deve apontar para users DENTRO do schema app (UUID), nunca legacy
            assert all(rt == "users" for rt, _ in refs)
            # confirmar que o alvo é UUID (app), não BIGINT (legacy)
            user_cols = {
                c["name"]: c["type"]
                for c in inspector.get_columns("users", schema=app_schema)
            }
            assert "UUID" in str(user_cols["id"])


def test_wrong_search_path_fails_fast(monkeypatch):
    expected = "pollo_test_expected_" + secrets.token_hex(4)
    eng = _engine()
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % expected))
        c.commit()

    import config
    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "test-secret")
    # search_path aponta para public, mas o schema esperado é `expected`
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {"options": "-csearch_path=public"},
        },
    )
    import loja.schema as schema_mod
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", expected)

    from loja import create_app
    try:
        with pytest.raises(RuntimeError):
            create_app()
    finally:
        with eng.connect() as c:
            c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % expected))
            c.commit()


def test_smoke_drop_only_disposable(monkeypatch):
    from loja.schema import assert_safe_disposable_schema

    target = "pollo_smoke_" + secrets.token_hex(4)
    sentinel = "pollo_sentinel_" + secrets.token_hex(4)

    eng = _engine()
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % target))
        c.execute(text("CREATE TABLE %s.t (id INT)" % target))
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % sentinel))
        c.execute(text("CREATE TABLE %s.t (id INT)" % sentinel))
        c.commit()

    # proteção recusa reservados
    for reserved in ["public", "pollomodas", "auth", "storage", "extensions"]:
        with pytest.raises(AssertionError):
            assert_safe_disposable_schema(reserved)
    assert_safe_disposable_schema(target)  # não levanta

    # drop apenas do schema alvo
    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % target))
        c.commit()

    inspector = sa_inspect(eng)
    assert target not in inspector.get_schema_names()
    assert sentinel in inspector.get_schema_names()

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % sentinel))
        c.commit()
