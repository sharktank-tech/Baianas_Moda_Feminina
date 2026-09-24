"""Testes unitários do script verify_payment_evidence_readonly (sem PG).

Cobre as três correções de revisão da PR #75:
1. sucesso exige mp_approved_valid_installments > 0;
2. cast numérico protegido por CASE WHEN;
3. make_url / engine.connect capturados → exit 2 + mensagem sanitizada.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_payment_evidence_readonly.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_payment_evidence_readonly_unit", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


# ---------------------------------------------------------------------------
# 1) classify_payment_evidence — OK só com MP aprovado válido
# ---------------------------------------------------------------------------


def test_classify_ok_requires_mp_approved_valid_positive():
    status, code = mod.classify_payment_evidence(
        {
            "total_payments": 5,
            "valid_installments_gte_1": 5,
            "mp_approved_valid_installments": 1,
        }
    )
    assert (status, code) == ("OK", 0)


def test_classify_zero_mp_approved_valid_is_fail_even_with_type_valid_rows():
    """Cenário obrigatório: zero parcelas válidas aprovadas no MP → FAIL."""
    status, code = mod.classify_payment_evidence(
        {
            "total_payments": 4,
            "valid_installments_gte_1": 3,
            "mp_approved_valid_installments": 0,
        }
    )
    assert (status, code) == ("FAIL", 1)


def test_classify_only_pending_or_non_mp_rows_is_fail():
    # pending MP com installments válido, ou provider diverso → mp_approved 0
    status, code = mod.classify_payment_evidence(
        {
            "total_payments": 2,
            "valid_installments_gte_1": 2,
            "mp_approved_valid_installments": 0,
        }
    )
    assert (status, code) == ("FAIL", 1)


def test_classify_empty_table_is_skip_not_ok():
    status, code = mod.classify_payment_evidence(
        {
            "total_payments": 0,
            "valid_installments_gte_1": 0,
            "mp_approved_valid_installments": 0,
        }
    )
    assert (status, code) == ("SKIP", 0)


def test_classify_negative_or_missing_mp_count_treated_as_fail():
    status, code = mod.classify_payment_evidence(
        {
            "total_payments": 1,
            "mp_approved_valid_installments": None,
        }
    )
    assert (status, code) == ("FAIL", 1)


def test_classify_does_not_use_type_valid_count_for_success():
    """Garante que valid_installments_gte_1 isolado não decide OK."""
    status, code = mod.classify_payment_evidence(
        {
            "total_payments": 10,
            "valid_installments_gte_1": 10,
            "mp_approved_valid_installments": 0,
        }
    )
    assert status != "OK"
    assert code == 1


# ---------------------------------------------------------------------------
# 2) Cast numérico protegido por CASE WHEN no SQL
# ---------------------------------------------------------------------------


def test_payments_sql_wraps_numeric_cast_in_case_when():
    sql = mod.build_payments_audit_sql("pollomodas")
    assert "CASE" in sql
    assert "THEN" in sql
    assert "ELSE NULL" in sql
    # o cast ::numeric só aparece no ramo THEN do CASE
    assert "WHEN jsonb_typeof" in sql
    assert "::numeric" in sql


def test_numeric_cast_not_bare_outside_case_branch():
    """Nenhum `::text::numeric` solto sem CASE por perto (estrutura global)."""
    sql = mod.build_payments_audit_sql("pollomodas")
    # todas as ocorrências de cast numérico devem estar dentro de um CASE
    # (a query usa a expressão CASE reutilizada em FILTER e min/max)
    assert sql.count("::text::numeric") >= 1
    assert "CASE" in sql
    # min/max não usam cast direto sem CASE
    assert "min((raw_payload -> 'installments')::text::numeric)" not in sql
    assert "max((raw_payload -> 'installments')::text::numeric)" not in sql


def test_installments_predicate_uses_case_numeric():
    col = "(raw_payload::jsonb)"
    pred = mod._installments_valid_predicate(col)
    assert "CASE" in pred
    assert "THEN" in pred
    assert "ELSE NULL" in pred
    assert ">= 1" in pred


def test_installments_numeric_case_rejects_string_and_null_branches():
    expr = mod._INSTALLMENTS_NUMERIC_CASE.format(col="(raw_payload::jsonb)")
    # ramo THEN só após guardas de tipo number + dígitos
    assert "jsonb_typeof" in expr
    assert r"^[0-9]+$" in expr
    assert "ELSE NULL" in expr
    # cast só no THEN
    then_part = expr.split("THEN", 1)[1].split("ELSE", 1)[0]
    assert "::text::numeric" in then_part
    else_part = expr.split("ELSE", 1)[1]
    assert "::numeric" not in else_part


# ---------------------------------------------------------------------------
# 3) make_url / engine.connect → exit 2 + mensagem sanitizada
# ---------------------------------------------------------------------------


def test_invalid_database_url_exits_2_without_password(monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL",
        "not-a-url-super-secret-token-xyz",
    )
    monkeypatch.setenv("POLLOMODAS_SCHEMA", "pollomodas")
    code = mod.main()
    captured = capsys.readouterr()
    assert code == 2
    assert "super-secret-token-xyz" not in captured.err
    assert "DATABASE_URL" in captured.err or "invalida" in captured.err


def test_engine_connect_failure_exits_2_sanitized(monkeypatch, capsys):
    """Falha de rede/banco em engine.connect() → exit 2, sem credencial."""
    secret = "pg-password-super-secret"

    class FakeEngine:
        def connect(self):
            raise ConnectionError(
                f"could not connect to server postgresql://user:{secret}@db"
            )

    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+psycopg2://user:{secret}@127.0.0.1:9/db",
    )
    monkeypatch.setenv("POLLOMODAS_SCHEMA", "pollomodas")
    monkeypatch.setattr(mod, "create_engine", lambda _url: FakeEngine())

    code = mod.main()
    captured = capsys.readouterr()
    assert code == 2
    assert secret not in captured.err
    assert "postgresql://" not in captured.err
    assert "ERRO" in captured.err
    assert "conexao" in captured.err.lower() or "falha" in captured.err.lower()
    # tipo da exceção pode aparecer; mensagem com URL não
    assert "could not connect" not in captured.err


def test_create_engine_failure_exits_2_sanitized(monkeypatch, capsys):
    secret = "another-secret-pass"

    def _boom(_url):
        raise RuntimeError(f"bad config for postgresql://x:{secret}@h/d")

    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+psycopg2://x:{secret}@127.0.0.1:5432/d",
    )
    monkeypatch.setenv("POLLOMODAS_SCHEMA", "pollomodas")
    monkeypatch.setattr(mod, "create_engine", _boom)

    code = mod.main()
    captured = capsys.readouterr()
    assert code == 2
    assert secret not in captured.err
    assert "RuntimeError" in captured.err


def test_make_url_exception_exits_2(monkeypatch, capsys):
    def _bad_make_url(_url):
        raise ValueError("malformed URL with password=leakme")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("POLLOMODAS_SCHEMA", "pollomodas")
    monkeypatch.setattr(mod, "make_url", _bad_make_url)

    code = mod.main()
    captured = capsys.readouterr()
    assert code == 2
    assert "leakme" not in captured.err
    assert "DATABASE_URL" in captured.err


def test_missing_database_url_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = mod.main()
    captured = capsys.readouterr()
    assert code == 2
    assert "DATABASE_URL" in captured.err
