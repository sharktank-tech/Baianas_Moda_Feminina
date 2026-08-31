"""Testes do gate de bootstrap de banco em create_app().

Garante que em cold start serverless (Vercel) NÃO abrimos conexão nem rodamos
DDL/bootstrap, mas o hook de search_path continua instalado; e que em ambiente
local/bootstrap explícito o bootstrap roda.
"""

from unittest.mock import MagicMock

import pytest

import loja
from loja import create_app
from loja import schema as schema_mod


def _clear_env(monkeypatch):
    for var in ("VERCEL", "POLLOMODAS_BOOTSTRAP_ON_STARTUP"):
        monkeypatch.delenv(var, raising=False)


def test_gate_explicit_absent_vercel_false(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    assert loja._should_bootstrap_database_on_startup() is False


def test_gate_local_default_true(monkeypatch):
    _clear_env(monkeypatch)
    assert loja._should_bootstrap_database_on_startup() is True


def test_gate_vercel_explicit_true(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("POLLOMODAS_BOOTSTRAP_ON_STARTUP", "1")
    assert loja._should_bootstrap_database_on_startup() is True


def test_gate_local_explicit_false(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POLLOMODAS_BOOTSTRAP_ON_STARTUP", "0")
    assert loja._should_bootstrap_database_on_startup() is False


def test_vercel_no_bootstrap(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")  # POLLOMODAS_BOOTSTRAP_ON_STARTUP ausente

    calls = {"bootstrap": 0, "hook": 0, "create_all": 0, "ensure_schema": 0}

    def fake_bootstrap(app):
        calls["bootstrap"] += 1

    def fake_hook(engine, schema_name):
        calls["hook"] += 1

    monkeypatch.setattr(loja, "_bootstrap_database", fake_bootstrap)
    monkeypatch.setattr(schema_mod, "install_search_path_hook", fake_hook)
    monkeypatch.setattr(schema_mod, "ensure_schema", MagicMock())
    monkeypatch.setattr(loja.db, "create_all", MagicMock())
    # Simula PostgreSQL sem precisar de um banco real.
    monkeypatch.setattr(loja, "is_postgresql_database", lambda: True)

    create_app()

    # Hook de search_path SEMPRE instalado em PostgreSQL.
    assert calls["hook"] == 1, "install_search_path_hook deve rodar em Vercel/PG"
    # Em cold start serverless, nada de bootstrap/conexão/DDL.
    assert calls["bootstrap"] == 0, "bootstrap NÃO deve rodar em cold start Vercel"
    assert loja.db.create_all.call_count == 0, "db.create_all NÃO em Vercel"
    assert schema_mod.ensure_schema.call_count == 0, "ensure_schema NÃO em Vercel"


def test_local_postgresql_bootstrap_runs(monkeypatch):
    _clear_env(monkeypatch)  # sem VERCEL, sem override => bootstrap TRUE

    calls = {"bootstrap": 0, "hook": 0}

    def fake_bootstrap(app):
        calls["bootstrap"] += 1

    def fake_hook(engine, schema_name):
        calls["hook"] += 1

    monkeypatch.setattr(loja, "_bootstrap_database", fake_bootstrap)
    monkeypatch.setattr(schema_mod, "install_search_path_hook", fake_hook)
    monkeypatch.setattr(loja, "is_postgresql_database", lambda: True)

    create_app()

    assert calls["hook"] == 1, "hook instalado em PostgreSQL local"
    assert calls["bootstrap"] == 1, "bootstrap roda em ambiente local PostgreSQL"
