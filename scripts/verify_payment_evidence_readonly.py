#!/usr/bin/env python3
"""Verificação somente leitura de evidência de modalidade e números de produto.

Cobre as pendências P1 e P6 da auditoria de `payment_scenario`:

- P1: em `payments.raw_payload`, a chave `installments` existe e é inteiro
  (estatísticas agregadas; nenhum payload individual é impresso);
- P6: contagens de produtos com preço/landed/estoque para revalidar a
  auditoria anterior (somente agregados).

Garantias:
  - SOMENTE LEITURA: transação READ ONLY + ROLLBACK;
  - schema-qualificado (padrão `pollomodas`); nunca assume `search_path`;
  - sem PII, sem tokens, sem payloads crus, sem IDs de cliente.

Uso:
  DATABASE_URL=postgresql://... python scripts/verify_payment_evidence_readonly.py
  POLLOMODAS_SCHEMA=pollomodas DATABASE_URL=postgresql://... python ...
"""

from __future__ import annotations

import os
import re
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


SCHEMA_ENV = "POLLOMODAS_SCHEMA"
DEFAULT_SCHEMA = "pollomodas"
SCHEMA_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _schema() -> str:
    schema = os.environ.get(SCHEMA_ENV) or DEFAULT_SCHEMA
    if not SCHEMA_RE.match(schema):
        raise SystemExit(f"schema invalido: {schema!r}")
    if schema in ("public", "auth", "storage", "extensions"):
        raise SystemExit(f"schema destrutivo/proibido: {schema!r}")
    return schema


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERRO: defina DATABASE_URL (postgresql://...)", file=sys.stderr)
        return 2

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        print(
            f"ERRO: DATABASE_URL nao e postgresql ({url.get_backend_name()})",
            file=sys.stderr,
        )
        return 2

    schema = _schema()
    engine = create_engine(database_url)

    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            ro = conn.execute(
                text("SELECT current_setting('transaction_read_only')")
            ).scalar()
            if str(ro).lower() != "on":
                print(f"FAIL: transaction_read_only != on ({ro!r})", file=sys.stderr)
                trans.rollback()
                return 2
            conn.execute(text(f"SET search_path TO {schema}"))
        except Exception as exc:
            try:
                trans.rollback()
            except Exception:
                pass
            print(f"FAIL: nao foi possivel estabelecer READ ONLY: {exc}", file=sys.stderr)
            return 2

        try:
            payments = conn.execute(
                text(
                    f"""
                    SELECT
                      count(*)::bigint AS total_payments,
                      count(*) FILTER (
                        WHERE raw_payload IS NOT NULL
                          AND raw_payload ? 'installments'
                      )::bigint AS with_installments_key,
                      count(*) FILTER (
                        WHERE pg_typeof(raw_payload->'installments') IN
                              ('integer'::regtype, 'bigint'::regtype)
                      )::bigint AS integer_installments,
                      count(*) FILTER (
                        WHERE raw_payload ? 'installments'
                          AND jsonb_typeof(
                            CASE WHEN pg_typeof(raw_payload) = 'jsonb'::regtype
                                 THEN raw_payload
                                 ELSE raw_payload::jsonb
                            END -> 'installments'
                          ) = 'number'
                          AND (raw_payload->>'installments') ~ '^[0-9]+$'
                      )::bigint AS nonneg_integer_text,
                      count(*) FILTER (
                        WHERE raw_payload ? 'installments'
                          AND (raw_payload->>'installments') ~ '^[0-9]+$'
                          AND (raw_payload->>'installments)::int >= 1
                      )::bigint AS installments_gte_1,
                      min(NULLIF(raw_payload->>'installments', '')
                          FILTER (WHERE raw_payload->>'installments' ~ '^[0-9]+$')
                      ) AS installments_min,
                      max(NULLIF(raw_payload->>'installments', '')
                          FILTER (WHERE raw_payload->>'installments' ~ '^[0-9]+$')
                      ) AS installments_max
                    FROM {schema}.payments
                    """
                )
            ).mappings().one()
        except Exception as exc:
            trans.rollback()
            print(f"FAIL: consulta payments: {exc}", file=sys.stderr)
            return 2

        try:
            products = conn.execute(
                text(
                    f"""
                    SELECT
                      count(*)::bigint AS total_products,
                      count(*) FILTER (WHERE price IS NOT NULL AND price > 0)::bigint
                        AS with_positive_price,
                      count(*) FILTER (WHERE is_active)::bigint AS active_products,
                      count(*) FILTER (WHERE is_draft)::bigint AS draft_products
                    FROM {schema}.products
                    """
                )
            ).mapping().one()
        except Exception as exc:
            trans.rollback()
            print(f"FAIL: consulta products: {exc}", file=sys.stderr)
            return 2

        try:
            links = conn.execute(
                text(
                    f"""
                    SELECT
                      count(*)::bigint AS total_active_links,
                      count(*) FILTER (
                        WHERE landed_cost IS NOT NULL AND landed_cost > 0
                      )::bigint AS with_landed_cost
                    FROM {schema}.supplier_products
                    WHERE is_active
                    """
                )
            ).mapping().one()
        except Exception as exc:
            trans.rollback()
            print(f"FAIL: consulta supplier_products: {exc}", file=sys.stderr)
            return 2

        try:
            stock = conn.execute(
                text(
                    f"""
                    SELECT
                      count(*)::bigint AS total_variants,
                      count(*) FILTER (WHERE stock > 0)::bigint AS variants_in_stock,
                      coalesce(sum(stock) FILTER (WHERE stock > 0), 0)::bigint
                        AS total_stock_units
                    FROM {schema}.product_variants
                    WHERE is_active
                    """
                )
            ).mapping().one()
        except Exception as exc:
            trans.rollback()
            print(f"FAIL: consulta product_variants: {exc}", file=sys.stderr)
            return 2

        trans.rollback()

    print("== P1: evidencia installments em payments.raw_payload ==")
    print(f"schema: {schema}")
    for key, value in dict(payments).items():
        print(f"  {key}: {value}")

    p1_ok = (
        payments["total_payments"] == 0
        or payments["installments_gte_1"] > 0
    )
    if payments["total_payments"] and payments["installments_gte_1"] == 0:
        print(
            "P1 FAIL: ha payments mas nenhuma linha com installments inteiro >= 1",
            file=sys.stderr,
        )
    elif p1_ok:
        print(
            "P1 OK (agregado): installments inteiro >= 1 presente"
            if payments["total_payments"]
            else "P1 SKIP: tabela payments vazia"
        )

    print("\n== P6: numeros de produto/custo/estoque (agregados) ==")
    for key, value in dict(products).items():
        print(f"  {key}: {value}")
    for key, value in dict(links).items():
        print(f"  {key}: {value}")
    for key, value in dict(stock).items():
        print(f"  {key}: {value}")

    if not p1_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
