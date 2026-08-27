"""Testes da guarda de schema descartável (sem banco de dados).

Garante que `assert_safe_disposable_schema` recusa nomes reservados do Supabase
(public/pollomodas/auth/storage/extensions) e exige o prefixo `pollo_smoke_`.
Parte das defesas do smoke e dos testes de isolamento.
"""
import pytest

from loja.schema import assert_safe_disposable_schema

RESERVED = {"public", "pollomodas", "auth", "storage", "extensions"}


def test_rejects_reserved_schemas():
    for name in RESERVED:
        with pytest.raises(AssertionError):
            assert_safe_disposable_schema(name)


def test_requires_pollo_smoke_prefix():
    with pytest.raises(AssertionError):
        assert_safe_disposable_schema("my_schema")
    with pytest.raises(AssertionError):
        assert_safe_disposable_schema("pollomodas_test")


def test_accepts_valid_disposable_schema():
    # não deve levantar
    assert_safe_disposable_schema("pollo_smoke_abc123")
    assert_safe_disposable_schema("pollo_smoke_" + "x" * 16)
