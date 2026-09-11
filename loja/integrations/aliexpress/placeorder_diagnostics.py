"""Telemetria sanitizada do placeorder AliExpress — FASE 8.0.11J/8.0.11L.

Este módulo existe para responder perguntas operacionais sobre uma tentativa de
``aliexpress.trade.buy.placeorder`` sem registrar payload, PII ou credenciais.

REGRAS DE SEGURANÇA:
- nunca registrar app_key/app_secret/access_token/session/sign;
- nunca registrar payload, endereço, nome, telefone ou CEP;
- nunca registrar remote order IDs;
- códigos de outcome são exclusivamente allow-listed;
- códigos remotos só passam se tiverem formato estrito de machine-code;
- mensagens remotas livres NUNCA são registradas;
- metadados numéricos/booleanos descrevem apenas forma, cardinalidade e tempo.

O ``ContextVar`` abaixo carrega somente um código de outcome allow-listed durante
a mesma execução lógica. Ele não contém identidade, token ou payload e é
consumido pelo boundary controlado após o core persistir o estado ``unknown``.

FASE 8.0.11L adiciona ``remote_error_code``/``remote_error_sub_code`` apenas aos
logs sanitizados. Valores livres são descartados, não truncados, para evitar que
texto remoto arbitrário seja promovido a telemetria confiável.
"""

from __future__ import annotations

import logging
import re
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

# Machine-code estrito: cobre códigos TOP observados/esperados como
# ``400``, ``INVALID_PARAMETER``, ``ISV.PARAMETER-ERROR`` e
# ``isv.invalid-permission``. Espaço, ``=``, ``:``, barras, aspas e qualquer
# outro caractere de texto livre são recusados integralmente.
_REMOTE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

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


def sanitize_remote_code(value: Any) -> str | None:
    """Aceita somente um machine-code remoto curto e sem texto livre.

    Não tenta "limpar" conteúdo potencialmente sensível. Se o valor não casar
    integralmente com o formato seguro, ele é descartado.
    """
    if value is None or isinstance(value, bool):
        return None
    code = str(value).strip()
    if not code or _REMOTE_CODE_RE.fullmatch(code) is None:
        return None
    return code


def response_error_metadata(response: Any) -> dict[str, str]:
    """Extrai somente códigos remotos seguros de uma resposta normalizada.

    ``error_message``, ids remotos e qualquer outro atributo são deliberadamente
    ignorados. O retorno pode ser expandido diretamente em ``log_placeorder_event``.
    """
    safe: dict[str, str] = {}
    code = sanitize_remote_code(getattr(response, "error_code", None))
    sub_code = sanitize_remote_code(getattr(response, "error_sub_code", None))
    if code is not None:
        safe["remote_error_code"] = code
    if sub_code is not None:
        safe["remote_error_sub_code"] = sub_code
    return safe


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
    proibidas. ``outcome_code`` passa por allow-list; os dois códigos remotos
    passam pelo formato estrito de machine-code.
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

    for key in ("remote_error_code", "remote_error_sub_code"):
        code = sanitize_remote_code(fields.get(key))
        if code is not None:
            safe[key] = code

    rendered = " ".join(f"{key}={value}" for key, value in safe.items())
    logger.info("aliexpress_placeorder_diagnostic %s", rendered)


__all__ = [
    "UNKNOWN_OUTCOME_CODES",
    "sanitize_unknown_code",
    "sanitize_remote_code",
    "response_error_metadata",
    "record_unknown_outcome",
    "consume_unknown_outcome",
    "clear_unknown_outcome",
    "request_shape",
    "log_placeorder_event",
]
