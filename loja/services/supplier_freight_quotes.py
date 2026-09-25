"""Persistência canônica de cotação de frete para cenário de referência.

Este módulo não conhece nem importa compra/fulfillment. Uma consulta externa
só ocorre por provider explicitamente injetado. O GET usa apenas leitura.
Somente o prefixo de três dígitos do CEP é persistido.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Mapping, Optional

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from loja import db
from loja.services.shipping_quote import (
    QuoteUnavailable,
    normalize_cep,
    validate_reference_fresh_quote,
)

# utcnow é importado DENTRO das funções (nunca no topo): o relógio precisa
# resolver `loja.utils.utcnow` no momento da chamada, senão um freeze de
# teste vigente no primeiro import congela este módulo para sempre e as
# janelas de validade passam a usar o tempo errado.

_TABLE = "supplier_reference_freight_quotes"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReferenceQuoteUnavailable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _naive_utc(value):
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _table_parts():
    if db.engine.dialect.name == "sqlite":
        return None, _TABLE
    schema = str(current_app.config.get("POLLOMODAS_SCHEMA") or "pollomodas")
    if not _SCHEMA_RE.fullmatch(schema) or schema == "public":
        raise ReferenceQuoteUnavailable("invalid_schema")
    return schema, _TABLE


def _qualified_table():
    schema, table = _table_parts()
    return f"{schema}.{table}" if schema else table


def _table_exists() -> bool:
    schema, table = _table_parts()
    return inspect(db.engine).has_table(table, schema=schema)


def _coerce_row_datetime(value):
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, str):
        try:
            return _naive_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _reference_scenario(dest_cep, option, qty):
    """Valida o cenário antes de chamar provider ou acessar a persistência."""
    dest = normalize_cep(dest_cep)
    if (
        dest is None
        or isinstance(qty, bool)
        or not isinstance(qty, (int, str))
        or not re.fullmatch(r"[0-9]{1,10}", str(qty))
        or not 1 <= int(qty) <= 2147483647
        or (option is not None and not isinstance(option, str))
        or (isinstance(option, str) and len(option) > 120)
    ):
        raise ReferenceQuoteUnavailable("invalid_quote")
    return dest, option or None, int(qty)


def find_valid_reference_quotes(*, dest_cep, option=None, qty=1, now=None) -> dict:
    """Carrega uma fotografia válida em uma única consulta, sem provider."""
    try:
        dest, option, qty = _reference_scenario(dest_cep, option, qty)
    except ReferenceQuoteUnavailable:
        return {}
    if not _table_exists():
        return {}
    from loja.utils import utcnow

    current = _naive_utc(now or utcnow())
    option_key = str(option or "")
    rows = db.session.execute(
        text(
            f"""SELECT supplier_product_id, dest_prefix, option_key, quantity,
                       currency, cost, min_days, max_days, quoted_at, expires_at
                FROM {_qualified_table()}
                WHERE quantity = :qty
                  AND dest_prefix = :dest_prefix
                  AND option_key = :option_key
                  AND expires_at > :now
                ORDER BY supplier_product_id"""
        ),
        {"qty": qty, "dest_prefix": dest[:3], "option_key": option_key, "now": current},
    ).mappings().all()
    snapshot = {}
    for row in rows:
        quoted_at = _coerce_row_datetime(row["quoted_at"])
        expires_at = _coerce_row_datetime(row["expires_at"])
        if quoted_at is None or expires_at is None or expires_at <= quoted_at:
            continue
        fresh = {
            "dest_prefix": row["dest_prefix"],
            "option": row["option_key"] or None,
            "qty": int(row["quantity"]),
            "currency": row["currency"],
            "cost": row["cost"],
            "min_days": row["min_days"],
            "max_days": row["max_days"],
            "quoted_at": quoted_at,
            "ttl_seconds": int((expires_at - quoted_at).total_seconds()),
        }
        try:
            validated_cost = validate_reference_fresh_quote(
                fresh, dest_cep=dest, option=option, qty=qty, now=current
            )
        except QuoteUnavailable:
            continue
        fresh["cost"] = validated_cost
        snapshot[str(row["supplier_product_id"])] = fresh
    return snapshot


def store_reference_freight_quote(
    *,
    supplier_product_id,
    dest_cep,
    option=None,
    qty=1,
    quote: Mapping,
    provider_method: str,
    evidence_ref: Optional[str] = None,
    now=None,
) -> dict:
    """Valida e grava uma única cotação vigente por cenário."""
    dest, option, qty = _reference_scenario(dest_cep, option, qty)
    if not isinstance(quote, Mapping):
        raise ReferenceQuoteUnavailable("invalid_quote")
    from loja.utils import utcnow

    current = _naive_utc(now or utcnow())
    fresh = dict(quote)
    # O provider deve comprovar o cenário: nunca preencher ou sobrescrever
    # esses campos com os valores solicitados antes de validar a resposta.
    if not {"dest_prefix", "option", "qty"}.issubset(fresh):
        raise ReferenceQuoteUnavailable("invalid_quote")
    fresh["quoted_at"] = _naive_utc(fresh.get("quoted_at"))
    try:
        cost = validate_reference_fresh_quote(
            fresh, dest_cep=dest, option=option, qty=qty, now=current
        )
        ttl_seconds = int(fresh["ttl_seconds"])
        expires_at = fresh["quoted_at"] + timedelta(seconds=ttl_seconds)
    except (QuoteUnavailable, TypeError, ValueError, KeyError, OverflowError):
        raise ReferenceQuoteUnavailable("invalid_quote") from None
    quoted_at = fresh["quoted_at"]
    if expires_at <= quoted_at:
        raise ReferenceQuoteUnavailable("invalid_window")
    method = str(provider_method or "").strip()
    if not method or len(method) > 120:
        raise ReferenceQuoteUnavailable("invalid_provider")
    if not _table_exists():
        raise ReferenceQuoteUnavailable("storage_unavailable")
    ref_hash = hashlib.sha256(str(evidence_ref).encode()).hexdigest() if evidence_ref else None
    table = _qualified_table()
    try:
        db.session.execute(
            text(
                f"""INSERT INTO {table}
                (supplier_product_id, dest_prefix, option_key, quantity,
                 currency, cost, min_days, max_days, quoted_at, expires_at,
                 provider_method, evidence_ref_hash, updated_at)
                VALUES
                (:supplier_product_id, :dest_prefix, :option_key, :quantity,
                 :currency, :cost, :min_days, :max_days, :quoted_at, :expires_at,
                 :provider_method, :evidence_ref_hash, :updated_at)
                ON CONFLICT
                (supplier_product_id, dest_prefix, option_key, quantity)
                DO UPDATE SET
                    currency = excluded.currency, cost = excluded.cost,
                    min_days = excluded.min_days, max_days = excluded.max_days,
                    quoted_at = excluded.quoted_at, expires_at = excluded.expires_at,
                    provider_method = excluded.provider_method,
                    evidence_ref_hash = excluded.evidence_ref_hash,
                    updated_at = excluded.updated_at
                WHERE excluded.quoted_at >= {table}.quoted_at"""
        ),
        {
            "supplier_product_id": str(supplier_product_id),
            "dest_prefix": dest[:3],
            "option_key": str(option or ""),
            "quantity": int(qty),
            "currency": "BRL",
            "cost": format(Decimal(cost), "f"),
            "min_days": fresh.get("min_days"),
            "max_days": fresh.get("max_days"),
            "quoted_at": quoted_at,
            "expires_at": expires_at,
            "provider_method": method,
            "evidence_ref_hash": ref_hash,
            "updated_at": current,
            },
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise ReferenceQuoteUnavailable("storage_error") from None
    return fresh


def request_reference_freight_quote(
    *, supplier_product_id, dest_cep, provider: Callable, option=None, qty=1, now=None
) -> dict:
    """Executa somente o provider injetado e persiste resposta validada."""
    dest_cep, option, qty = _reference_scenario(dest_cep, option, qty)
    if not callable(provider):
        raise ReferenceQuoteUnavailable("provider_unavailable")
    try:
        response = provider(
            supplier_product_id=supplier_product_id,
            dest_cep=dest_cep,
            option=option,
            qty=int(qty),
        )
    except Exception:
        raise ReferenceQuoteUnavailable("provider_unavailable") from None
    if not isinstance(response, Mapping):
        raise ReferenceQuoteUnavailable("invalid_quote")
    return store_reference_freight_quote(
        supplier_product_id=supplier_product_id,
        dest_cep=dest_cep,
        option=option,
        qty=qty,
        quote=response,
        provider_method=str(response.get("provider_method") or ""),
        evidence_ref=response.get("evidence_ref"),
        now=now,
    )


__all__ = [
    "ReferenceQuoteUnavailable",
    "find_valid_reference_quotes",
    "store_reference_freight_quote",
    "request_reference_freight_quote",
]
