#!/usr/bin/env python3
"""Verificação somente leitura de evidência de modalidade e números de produto.

Cobre as pendências P1 e P6 da auditoria de `payment_scenario`:

- P1: em ``payments.raw_payload``, a chave ``installments`` existe e satisfaz o
  contrato do resolvedor (inteiro JSON >= 1; rejeita boolean, string, fração,
  zero, negativo e ausência) — estatísticas agregadas, sem payload individual;
- P6: contagens de produtos com preço/landed/estoque (somente agregados).

Contrato do resolvedor (``resolve_payment_scenario``): Python ``int`` >= 1
vindo de JSON; ``bool``/``str``/``float``/ausente → desconhecido.

Garantias:
  - SOMENTE LEITURA: transação READ ONLY verificada + ROLLBACK;
  - consultas qualificadas pelo schema (padrão ``pollomodas``);
  - saída agregada, sem payloads, tokens, PII ou credenciais;
  - erros de URL/conexão são capturados e redigidos (senha nunca impressa);
  - sucesso (OK) exige ``mp_approved_valid_installments > 0``.

Códigos de saída:
  - 0: OK (evidência MP aprovada válida) ou SKIP (tabela vazia, inconclusivo);
  - 1: há payments mas zero evidência aprovada válida no Mercado Pago (FAIL);
  - 2: configuração/URL/conexão/consulta/transação inválida.

Uso:
  DATABASE_URL=postgresql://... python scripts/verify_payment_evidence_readonly.py
  POLLOMODAS_SCHEMA=pollomodas DATABASE_URL=postgresql://... python ...
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Mapping, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


SCHEMA_ENV = "BAIANAS_DB_SCHEMA"
DEFAULT_SCHEMA = "baianas_moda_feminina"
SCHEMA_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Cast numérico protegido: só converte quando o tipo é number e os dígitos
# batem com o regex; caso contrário NULL. Não depende de short-circuit AND.
_INSTALLMENTS_NUMERIC_CASE = (
    "(CASE"
    " WHEN jsonb_typeof({col} -> 'installments') = 'number'"
    " AND ({col} -> 'installments')::text ~ '^[0-9]+$'"
    " THEN ({col} -> 'installments')::text::numeric"
    " ELSE NULL END)"
)

_INSTALLMENTS_VALID = (
    "jsonb_typeof({col} -> 'installments') = 'number'"
    " AND ({col} -> 'installments')::text ~ '^[0-9]+$'"
    " AND {numeric} >= 1"
)


def _schema() -> str:
    schema = os.environ.get(SCHEMA_ENV) or DEFAULT_SCHEMA
    if not SCHEMA_RE.match(schema):
        raise SystemExit(f"schema invalido: {schema!r}")
    if schema in ("public", "pollomodas", "auth", "storage", "extensions"):
        raise SystemExit(f"schema destrutivo/proibido: {schema!r}")
    return schema


def _redact(message: str, password: Optional[str]) -> str:
    if password:
        message = message.replace(password, "***")
    return message


def _err(message: str, password: Optional[str] = None) -> None:
    print(_redact(message, password), file=sys.stderr)


def _exc_label(exc: BaseException) -> str:
    """Rótulo sanitizado: só o tipo, nunca a mensagem completa (pode ter URL)."""
    return type(exc).__name__


def _jsonb_expr(column: str) -> str:
    return f"({column}::jsonb)"


def _installments_valid_predicate(col: str) -> str:
    numeric = _INSTALLMENTS_NUMERIC_CASE.format(col=col)
    return _INSTALLMENTS_VALID.format(col=col, numeric=numeric)


def build_payments_audit_sql(schema: str) -> str:
    """SQL agregado: gerais, MP elegíveis e evidência de modalidade."""
    col = "raw_payload"
    j = _jsonb_expr(col)
    valid = _installments_valid_predicate(j)
    safe_numeric = _INSTALLMENTS_NUMERIC_CASE.format(col=j)
    return f"""
    SELECT
      count(*)::bigint AS total_payments,
      count(*) FILTER (
        WHERE provider = 'mercado_pago'
      )::bigint AS mp_payments,
      count(*) FILTER (
        WHERE provider = 'mercado_pago'
          AND lower(status) = 'approved'
      )::bigint AS mp_approved_payments,
      count(*) FILTER (
        WHERE {col} IS NOT NULL
          AND {j} ? 'installments'
      )::bigint AS with_installments_key,
      count(*) FILTER (
        WHERE {valid}
      )::bigint AS valid_installments_gte_1,
      count(*) FILTER (
        WHERE provider = 'mercado_pago'
          AND lower(status) = 'approved'
          AND {valid}
      )::bigint AS mp_approved_valid_installments,
      min({safe_numeric}) AS installments_min,
      max({safe_numeric}) AS installments_max
    FROM {schema}.payments
    """


def build_products_audit_sql(schema: str) -> str:
    return f"""
    SELECT
      count(*)::bigint AS total_products,
      count(*) FILTER (
        WHERE price IS NOT NULL AND price > 0
      )::bigint AS with_positive_price,
      count(*) FILTER (WHERE is_active)::bigint AS active_products,
      count(*) FILTER (WHERE is_draft)::bigint AS draft_products
    FROM {schema}.products
    """


def build_links_audit_sql(schema: str) -> str:
    return f"""
    SELECT
      count(*)::bigint AS total_active_links,
      count(*) FILTER (
        WHERE landed_cost IS NOT NULL AND landed_cost > 0
      )::bigint AS with_landed_cost
    FROM {schema}.supplier_products
    WHERE is_active
    """


def build_stock_audit_sql(schema: str) -> str:
    return f"""
    SELECT
      count(*)::bigint AS total_variants,
      count(*) FILTER (WHERE stock > 0)::bigint AS variants_in_stock,
      coalesce(sum(stock) FILTER (WHERE stock > 0), 0)::bigint
        AS total_stock_units
    FROM {schema}.product_variants
    WHERE is_active
    """


def classify_payment_evidence(payments: Mapping[str, Any]) -> tuple[str, int]:
    """Retorna (status, exit_code) para a evidência de modalidade.

    - SKIP (0): tabela payments vazia — inconclusivo;
    - FAIL (1): há payments mas zero evidência aprovada válida no Mercado Pago
      (``mp_approved_valid_installments <= 0``);
    - OK (0): somente com ``mp_approved_valid_installments > 0``.
    """
    total = int(payments.get("total_payments") or 0)
    mp_approved_valid = int(payments.get("mp_approved_valid_installments") or 0)
    if total == 0:
        return "SKIP", 0
    if mp_approved_valid <= 0:
        return "FAIL", 1
    return "OK", 0


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        _err("ERRO: defina DATABASE_URL (postgresql://...)")
        return 2

    password: Optional[str] = None
    try:
        url = make_url(database_url)
        password = url.password
        backend = url.get_backend_name()
        if backend != "postgresql":
            _err(
                f"ERRO: DATABASE_URL nao e postgresql ({backend})",
                password,
            )
            return 2
        schema = _schema()
    except SystemExit:
        raise
    except Exception as exc:
        _err(f"ERRO: DATABASE_URL invalida ({_exc_label(exc)})", password)
        return 2

    try:
        engine = create_engine(database_url)
    except Exception as exc:
        _err(f"ERRO: falha ao criar engine ({_exc_label(exc)})", password)
        return 2

    payments: Optional[Mapping[str, Any]] = None
    products: Optional[Mapping[str, Any]] = None
    links: Optional[Mapping[str, Any]] = None
    stock: Optional[Mapping[str, Any]] = None

    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                ro = conn.execute(
                    text("SELECT current_setting('transaction_read_only')")
                ).scalar()
                if str(ro).lower() != "on":
                    _err(
                        f"FAIL: transaction_read_only != on ({ro!r})",
                        password,
                    )
                    trans.rollback()
                    return 2
                # Busca qualificada por schema permanece a fonte de verdade;
                # search_path é apenas compatibilidade adicional.
                conn.execute(text(f"SET search_path TO {schema}"))
            except Exception as exc:
                try:
                    trans.rollback()
                except Exception:
                    pass
                _err(
                    f"FAIL: nao foi possivel estabelecer READ ONLY "
                    f"({_exc_label(exc)})",
                    password,
                )
                return 2

            try:
                payments = conn.execute(
                    text(build_payments_audit_sql(schema))
                ).mappings().one()
                products = conn.execute(
                    text(build_products_audit_sql(schema))
                ).mappings().one()
                links = conn.execute(
                    text(build_links_audit_sql(schema))
                ).mappings().one()
                stock = conn.execute(
                    text(build_stock_audit_sql(schema))
                ).mappings().one()
            except Exception as exc:
                try:
                    trans.rollback()
                except Exception:
                    pass
                _err(
                    f"FAIL: consulta de auditoria ({_exc_label(exc)})",
                    password,
                )
                return 2

            try:
                trans.rollback()
            except Exception as exc:
                _err(f"FAIL: rollback ({_exc_label(exc)})", password)
                return 2
    except Exception as exc:
        # make_url/connect e falhas de rede/banco caem aqui com exit 2
        # e rótulo sanitizado (sem credenciais).
        _err(f"ERRO: falha de conexao ({_exc_label(exc)})", password)
        return 2

    if payments is None or products is None or links is None or stock is None:
        _err("ERRO: consulta de auditoria nao retornou resultado")
        return 2

    evidence_status, evidence_code = classify_payment_evidence(payments)

    print("== P1: evidencia installments em payments.raw_payload ==")
    print(f"schema: {schema}")
    print(f"status: {evidence_status}")
    for key, value in dict(payments).items():
        print(f"  {key}: {value}")

    if evidence_status == "SKIP":
        print(
            "P1 SKIP: tabela payments vazia — evidencia INCONCLUSIVA "
            "(nao confirma nem refuta o payload real)"
        )
    elif evidence_status == "FAIL":
        _err(
            "P1 FAIL: ha payments mas nenhuma evidencia valida aprovada "
            "no Mercado Pago (mp_approved_valid_installments = 0)"
        )
    else:
        print(
            "P1 OK (agregado): installments inteiro >= 1 em pagamento "
            "Mercado Pago aprovado (mp_approved_valid_installments > 0)"
        )

    print("\n== P6: numeros de produto/custo/estoque (agregados) ==")
    for key, value in dict(products).items():
        print(f"  {key}: {value}")
    for key, value in dict(links).items():
        print(f"  {key}: {value}")
    for key, value in dict(stock).items():
        print(f"  {key}: {value}")

    return evidence_code


if __name__ == "__main__":
    raise SystemExit(main())
