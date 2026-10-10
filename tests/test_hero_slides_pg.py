"""Migração 0040 em PostgreSQL real (sem skips no CI).

Parte de `hero_slides` legado (só colunas originais), aplica o arquivo 2x,
valida estrutura completa e confirma que slides existentes ganham os
defaults sem alteração silenciosa. Somente banco descartável loopback.
"""
from __future__ import annotations

import secrets
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    _url = make_url(DATABASE_URL) if DATABASE_URL else None
    IS_SAFE_POSTGRES = bool(
        _url
        and _url.get_backend_name() == "postgresql"
        and _url.host in {"127.0.0.1", "localhost", "::1"}
        and str(_url.database or "").startswith("pollo")
    )
except Exception:
    IS_SAFE_POSTGRES = False

pytestmark = pytest.mark.skipif(
    not IS_SAFE_POSTGRES,
    reason="requer PostgreSQL descartável loopback pollo*",
)


@pytest.fixture
def legacy_hero():
    schema = "pollo_test_hero_mig_%s" % secrets.token_hex(4)
    engine = create_engine(
        DATABASE_URL,
        connect_args={"options": "-csearch_path=%s" % schema},
    )
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE SCHEMA "%s"' % schema))
            conn.execute(text(
                "CREATE TABLE hero_slides ("
                "id uuid PRIMARY KEY, image text NOT NULL, "
                "alt varchar(255), eyebrow varchar(100), "
                "title varchar(100), link varchar(255), "
                "is_active boolean DEFAULT true, "
                '"order" integer DEFAULT 0, '
                "created_at timestamp without time zone)"
            ))
            slide_id = uuid.uuid4()
            conn.execute(
                text("INSERT INTO hero_slides (id, image, title) "
                     "VALUES (:id, 'https://exemplo.com/desk.jpg', 'Antigo')"),
                {"id": str(slide_id)},
            )
            migration = Path(
                "migrations/0040_hero_slides_responsive.sql").read_text()
            migration = migration.replace(
                "SET search_path TO pollomodas;",
                'SET search_path TO "%s";' % schema,
            )
            runnable = "\n".join(
                ln for ln in migration.splitlines()
                if not ln.lstrip().startswith("\\")
            )
            for _ in (1, 2):
                conn.exec_driver_sql(runnable)
            row = conn.execute(text(
                "SELECT image_mobile, focal_desktop, focal_mobile, "
                "content_position, description, show_title, "
                "show_description, title FROM hero_slides WHERE id = :id"
            ), {"id": str(slide_id)}).fetchone()
            assert row[1:] == ("center", "center", "left", None, True, True,
                               "Antigo"), row
            assert row[0] is None
        import psycopg2

        raw = psycopg2.connect(
            DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://"),
            connect_timeout=15,
        )
        raw.autocommit = True
        try:
            from migrations.validate_0040 import validate_schema

            assert validate_schema(raw, schema=schema) == []
        finally:
            raw.close()
        yield schema
    finally:
        with engine.begin() as conn:
            conn.execute(
                text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
        engine.dispose()


def test_0040_aplica_e_preserva_slide(legacy_hero):
    assert legacy_hero.startswith("pollo_test_hero_mig_")


def test_validate_0040_recusa_estrutura_incorreta():
    from migrations.validate_0040 import validate_schema

    class FakeCursor:
        def execute(self, query, params=None):
            if "information_schema" in query:
                self.rows = [
                    ("image_mobile", "text", True, None),
                    ("focal_desktop", "text", False, None),
                    ("focal_mobile", "character varying", False,
                     "'center'::character varying"),
                    ("content_position", "character varying", False,
                     "'left'::character varying"),
                    ("show_title", "boolean", False, "true"),
                    ("show_description", "boolean", False, "true"),
                ]
            else:
                self.rows = [(None,)]

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    errors = validate_schema(FakeConn())
    assert len(errors) >= 2, errors
    assert any("description" in e for e in errors)
    assert any("focal_desktop" in e for e in errors)
