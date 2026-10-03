"""Performance da home (PR #103/TTFB): cache de dados anônimo, recorte de
categorias no SQL e índice público do list.

Cobertura:
- cache TTL 60s para visitante anônimo (e leitura após invalidação manual);
- usuário logado nunca usa o cache (favoritos são por usuário);
- _get_home_categories: row_number() + LIMIT no SQL (não varre a tabela);
- snapshot cacheado não contém objeto ORM;
- template renderiza produto/promoção a partir dos snapshots (tipos nativos);
- model declara ix_products_public_recent (install novo via create_all);
- migration 0033 idempotente (up e down) no repositório.
"""

import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, inspect as sa_inspect

import loja.cache as _cache_mod
from loja.modules.services import _get_home_categories

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_anonymous_home_data_cached_until_invalidation(client, make_product):
    make_product(name="Produto Alpha Cache")

    r1 = client.get("/")
    assert r1.status_code == 200
    assert "home_data" in _cache_mod._cache
    assert "Produto Alpha Cache".encode() in r1.data

    make_product(name="Produto Beta Cache")
    r2 = client.get("/")
    # Servido do cache (TTL 60s): o produto criado depois ainda não aparece.
    assert "Produto Beta Cache".encode() not in r2.data

    _cache_mod._cache.pop("home_data", None)
    r3 = client.get("/")
    assert "Produto Beta Cache".encode() in r3.data


def test_authenticated_home_data_skips_cache(client, regular_user, login):
    login(regular_user.email)

    r = client.get("/")
    assert r.status_code == 200
    assert "home_data" not in _cache_mod._cache


def test_home_categories_window_limited_in_sql(db, make_product):
    for i in range(12):
        make_product(name="Cat %02d" % i, category="cat-%02d" % i)

    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        cats = _get_home_categories()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert len(cats) == 10
    slugs = [c["slug"] for c in cats]
    assert slugs == sorted(slugs)
    assert len(set(slugs)) == 10
    assert any("row_number" in s.lower() for s in statements), (
        "a janela de categorias deve acontecer no SQL, não no Python"
    )


def test_home_cache_snapshot_contains_no_orm(client, db):
    r = client.get("/")
    assert r.status_code == 200
    data, _deadline = _cache_mod._cache["home_data"]
    assert isinstance(data, dict)

    def _walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, "%s.%s" % (path, key))
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                _walk(value, "%s[%d]" % (path, i))
        else:
            assert not isinstance(node, db.Model), "ORM em %s" % path

    _walk(data, "home_data")


def test_home_renders_promotion_from_snapshot(client, db, make_product):
    from loja.modules.models import Promotion

    product = make_product(name="Promo Snapshot Shirt", price="80.00")
    db.session.add(
        Promotion(
            id=uuid.uuid4(),
            product_id=product.id,
            sale_price=Decimal("55.00"),
            is_active=True,
        )
    )
    db.session.commit()

    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Promo Snapshot Shirt" in body
    assert "55,00" in body  # sale_price (Decimal) formatado no card
    assert "80,00" in body  # preço original no card de desconto


def test_products_public_index_declared(db):
    idxs = sa_inspect(db.engine).get_indexes("products")
    match = [ix for ix in idxs if ix.get("name") == "ix_products_public_recent"]
    assert match, "ix_products_public_recent ausente: %r" % (idxs,)
    assert match[0]["column_names"] == ["is_active", "is_draft", "created_at"]


def test_migration_0033_declares_idempotent_index():
    sql = (_MIGRATIONS / "0033_products_public_recent_index.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS ix_products_public_recent" in sql
    assert "pollomodas.products (is_active, is_draft, created_at)" in sql

    down = (
        _MIGRATIONS / "0033_products_public_recent_index_down.sql"
    ).read_text()
    assert "DROP INDEX IF EXISTS pollomodas.ix_products_public_recent" in down
