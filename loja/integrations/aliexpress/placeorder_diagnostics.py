"""Telemetria sanitizada do placeorder AliExpress — FASE 8.0.11J.

Este módulo existe para responder uma pergunta operacional específica: em qual
etapa uma tentativa de ``aliexpress.trade.buy.placeorder`` terminou sem
identidade remota confirmada?

REGRAS DE SEGURANÇA:
- nunca registrar app_key/app_secret/access_token/session/sign;
- nunca registrar payload, endereço, nome, telefone ou CEP;
- nunca registrar remote order IDs;
- códigos de outcome são exclusivamente allow-listed;
- metadados numéricos/booleanos descrevem apenas forma, cardinalidade e tempo.

O ``ContextVar`` abaixo carrega somente um código allow-listed durante a mesma
execução lógica. Ele não contém identidade, token ou payload e é consumido pelo
boundary controlado após o core persistir o estado ``unknown``.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Mapping

logger = logging.getLogger("loja.integrations.aliexpress.placeorder.diagnostics")


UNKNOWN_OUTCOME_CODES = frozenset(
    {
        "placeorder_timeout",
        "placeorder_transport_error",
        "placeorder_protocol_error",
        "placeorder_remote_error",
        "placeorder_success_without_order_id",
        "placeorder_remote_identity_ambiguous",
        "placeorder_unexpected_error",
    }
)

SAFE_EVENT_NAMES = frozenset(
    {
        "request_ready",
        "provider_started",
        "provider_unknown",
        "provider_rejected",
        "provider_confirmed",
    }
)

_CURRENT_UNKNOWN_OUTCOME: ContextVar[str | None] = ContextVar(
    "aliexpress_placeorder_unknown_outcome",
    default=None,
)


def sanitize_unknown_code(value: Any) -> str:
    """Retorna somente códigos conhecidos; qualquer valor estranho vira fixo."""
    code = str(value or "").strip()
    if code in UNKNOWN_OUTCOME_CODES:
        return code
    return "placeorder_unexpected_error"


def record_unknown_outcome(value: Any) -> str:
    """Guarda no contexto atual somente o código sanitizado da tentativa."""
    code = sanitize_unknown_code(value)
    _CURRENT_UNKNOWN_OUTCOME.set(code)
    return code


def consume_unknown_outcome() -> str | None:
    """Consome e limpa o outcome do contexto para impedir vazamento entre calls."""
    code = _CURRENT_UNKNOWN_OUTCOME.get()
    _CURRENT_UNKNOWN_OUTCOME.set(None)
    return code


def clear_unknown_outcome() -> None:
    """Limpa explicitamente o contexto antes/depois de um boundary controlado."""
    _CURRENT_UNKNOWN_OUTCOME.set(None)


def request_shape(request: Any) -> dict[str, Any]:
    """Resume apenas a FORMA do request, nunca seus valores sensíveis."""
    shipping = getattr(request, "shipping_address", None)
    items = getattr(request, "items", None) or ()
    if not isinstance(shipping, Mapping):
        shipping = {}

    required_shipping = (
        "address",
        "city",
        "contact_person",
        "country",
        "mobile_no",
        "province",
        "zip",
    )

    return {
        "item_count": len(items),
        "shipping_required_present": sum(
            1 for key in required_shipping if str(shipping.get(key) or "").strip()
        ),
        "shipping_required_total": len(required_shipping),
        "has_logistics_service": any(
            bool(str(getattr(item, "logistics_service_name", None) or "").strip())
            for item in items
        ),
    }


def log_placeorder_event(event: str, **fields: Any) -> None:
    """Emite uma linha key=value com campos explicitamente sanitizados.

    Campos aceitos são reduzidos a tipos escalares seguros. Strings livres são
    proibidas, exceto ``outcome_code`` que passa por allow-list.
    """
    safe_event = event if event in SAFE_EVENT_NAMES else "provider_unknown"
    safe: dict[str, Any] = {"event": safe_event}

    for key in (
        "duration_ms",
        "item_count",
        "shipping_required_present",
        "shipping_required_total",
        "remote_order_count",
    ):
        value = fields.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            safe[key] = value

    for key in ("has_logistics_service", "is_success"):
        value = fields.get(key)
        if isinstance(value, bool):
            safe[key] = value

    if "outcome_code" in fields:
        safe["outcome_code"] = sanitize_unknown_code(fields.get("outcome_code"))

    rendered = " ".join(f"{key}={value}" for key, value in safe.items())
    logger.info("aliexpress_placeorder_diagnostic %s", rendered)


__all__ = [
    "UNKNOWN_OUTCOME_CODES",
    "sanitize_unknown_code",
    "record_unknown_outcome",
    "consume_unknown_outcome",
    "clear_unknown_outcome",
    "request_shape",
    "log_placeorder_event",
]
