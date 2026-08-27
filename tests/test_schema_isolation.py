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
from sqlalchemy.engine import make_url

from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

# O conftest sobrescreve os.environ["DATABASE_URL"] por SQLite para a suíte
# unitária. Usamos a URL original capturada pelo conftest para enxergar o
# PostgreSQL real fornecido no comando, sem exigir uma segunda variável.
from conftest import ORIGINAL_DATABASE_URL

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    IS_POSTGRES = (
        bool(DATABASE_URL)
        and make_url(DATABASE_URL).get_backend_name() == "postgresql"
    )
except Exception:
    IS_POSTGRES = False

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


def test_ensure_schema_adds_fk_when_column_exists(monkeypatch):
    """Regressão do Cenário B: coluna existe, FK ausente -> ADD CONSTRAINT
    idempotente (ramo PostgreSQL de ensure_schema). Antes do fix faltava o
    placeholder '%(t)s' e o DDL quebrava com KeyError, então o smoke nunca
    exercitava esse caminho na suíte SQLite."""
    app_schema = "pollo_test_app_" + secrets.token_hex(4)
    eng = _engine()
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % app_schema))
        # tabela pai referenciada
        c.execute(
            text("CREATE TABLE %s.categories (id UUID PRIMARY KEY)" % app_schema)
        )
        # tabela filha: category_id JÁ existe, mas SEM FK
        c.execute(
            text(
                "CREATE TABLE %s.products (id UUID PRIMARY KEY, category_id UUID)"
                % app_schema
            )
        )
        c.commit()

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

    from loja import create_app, db
    from loja.schema import ensure_schema
    app = create_app()  # internamente roda ensure_schema -> ADD CONSTRAINT
    with app.app_context():
        inspector = sa_inspect(db.engine)
        fks = inspector.get_foreign_keys("products", schema=app_schema)
        assert any(
            f.get("referred_table") == "categories"
            and "category_id" in (f.get("constrained_columns") or [])
            for f in fks
        ), "FK category_id -> categories não foi criada (Cenário B)"

        # segunda execução deve ser idempotente: exatamente 1 FK, sem erro
        ensure_schema(app)
        fks2 = inspector.get_foreign_keys("products", schema=app_schema)
        n = sum(
            1
            for f in fks2
            if f.get("referred_table") == "categories"
            and "category_id" in (f.get("constrained_columns") or [])
        )
        assert n == 1, "ensure_schema não foi idempotente (FK duplicada: %d)" % n

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % app_schema))
        c.commit()


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
    import config
    import loja.schema as schema_mod

    # Caso B: o pooler ignora options=-csearch_path (aqui apontando para public),
    # mas o hook de transação deve forçar DB_SCHEMA. Não deve cair silenciosamente
    # em public.
    expected = "pollo_test_expected_" + secrets.token_hex(4)
    eng = _engine()
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % expected))
        c.commit()

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {"options": "-csearch_path=public"},
        },
    )
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", expected)

    from loja import create_app, db
    app = create_app()  # não deve levantar
    with app.app_context():
        with db.engine.connect() as c:
            effective = c.execute(text("SELECT current_schema()")).scalar()
    assert effective == expected, (
        "hook de transação não forçou search_path=%s (current_schema=%r)"
        % (expected, effective)
    )

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % expected))
        c.commit()

    # Fail-fast: DB_SCHEMA proibido (public) deve ser rejeitado antes de qualquer DDL.
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", "public")
    with pytest.raises(RuntimeError):
        create_app()


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
