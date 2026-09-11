"""Telemetria sanitizada do placeorder AliExpress — FASE 8.0.11J/8.0.11L/8.0.11M/8.0.11N.

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

Os ``ContextVar`` abaixo carregam somente dados sanitizados durante a mesma
execução lógica. Eles não contêm identidade, token, payload ou mensagem remota.

FASE 8.0.11L adiciona ``remote_error_code``/``remote_error_sub_code`` aos logs
sanitizados. FASE 8.0.11M permite preservar os mesmos machine-codes sanitizados
no ``last_error`` da tentativa ``unknown`` para que o diagnóstico sobreviva à
retenção dos logs, sem alterar o erro público do ``AliExpressOrder``.

FASE 8.0.11N adiciona validação fail-closed do formato durável antes de ele ser
retido por uma reconciliação conclusiva. Valores legados/livres nunca são
carregados adiante apenas porque já estavam no banco.
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

_REMOTE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DURABLE_REMOTE_KEYS = ("remote_error_code", "remote_error_sub_code")
_MAX_DURABLE_ERROR_LENGTH = 256

_CURRENT_UNKNOWN_OUTCOME: ContextVar[str | None] = ContextVar(
    "aliexpress_placeorder_unknown_outcome",
    default=None,
)
_CURRENT_REMOTE_ERROR_METADATA: ContextVar[dict[str, str] | None] = ContextVar(
    "aliexpress_placeorder_remote_error_metadata",
    default=None,
)


def sanitize_unknown_code(value: Any) -> str:
    code = str(value or "").strip()
    if code in UNKNOWN_OUTCOME_CODES:
        return code
    return "placeorder_unexpected_error"


def sanitize_remote_code(value: Any) -> str | None:
    """Aceita somente um machine-code remoto curto e sem texto livre."""
    if value is None or isinstance(value, bool):
        return None
    code = str(value).strip()
    if not code or _REMOTE_CODE_RE.fullmatch(code) is None:
        return None
    return code


def response_error_metadata(response: Any) -> dict[str, str]:
    """Extrai somente códigos remotos seguros de uma resposta normalizada."""
    safe: dict[str, str] = {}
    code = sanitize_remote_code(getattr(response, "error_code", None))
    sub_code = sanitize_remote_code(getattr(response, "error_sub_code", None))
    if code is not None:
        safe["remote_error_code"] = code
    if sub_code is not None:
        safe["remote_error_sub_code"] = sub_code
    return safe


def record_remote_error_metadata(response: Any) -> dict[str, str]:
    """Guarda no contexto somente os machine-codes remotos já sanitizados."""
    safe = response_error_metadata(response)
    _CURRENT_REMOTE_ERROR_METADATA.set(dict(safe) if safe else None)
    return safe


def consume_remote_error_metadata() -> dict[str, str]:
    """Consome e limpa metadados remotos sanitizados do contexto atual."""
    metadata = _CURRENT_REMOTE_ERROR_METADATA.get() or {}
    _CURRENT_REMOTE_ERROR_METADATA.set(None)
    return dict(metadata)


def clear_remote_error_metadata() -> None:
    _CURRENT_REMOTE_ERROR_METADATA.set(None)


def record_unknown_outcome(value: Any) -> str:
    code = sanitize_unknown_code(value)
    _CURRENT_UNKNOWN_OUTCOME.set(code)
    return code


def consume_unknown_outcome() -> str | None:
    code = _CURRENT_UNKNOWN_OUTCOME.get()
    _CURRENT_UNKNOWN_OUTCOME.set(None)
    return code


def clear_unknown_outcome() -> None:
    _CURRENT_UNKNOWN_OUTCOME.set(None)


def build_durable_unknown_error(
    outcome_code: Any,
    remote_metadata: Mapping[str, Any] | None = None,
) -> str:
    """Monta um ``last_error`` durável usando apenas valores sanitizados.

    Os separadores/chaves são constantes locais. Valores remotos que não passam
    em ``sanitize_remote_code`` são simplesmente omitidos.
    """
    parts = [sanitize_unknown_code(outcome_code)]
    metadata = remote_metadata or {}
    for key in _DURABLE_REMOTE_KEYS:
        value = sanitize_remote_code(metadata.get(key))
        if value is not None:
            parts.append(f"{key}={value}")
    return "|".join(parts)


def validate_durable_unknown_error(value: Any) -> str | None:
    """Valida e canoniza um diagnóstico durável já persistido.

    Esta função é deliberadamente fail-closed. Ela aceita somente o formato que
    ``build_durable_unknown_error`` produz: um outcome allow-listed seguido de
    zero, um ou dois pares ``key=machine-code`` conhecidos, sem duplicatas.

    Qualquer texto livre, chave desconhecida, duplicata, whitespace interno,
    tamanho excessivo ou outcome desconhecido retorna ``None``. Isso permite que
    consumidores históricos preservem telemetria segura sem propagar conteúdo
    legado potencialmente sensível.
    """
    if value is None or isinstance(value, bool):
        return None
    raw = str(value)
    if not raw or len(raw) > _MAX_DURABLE_ERROR_LENGTH or raw != raw.strip():
        return None

    parts = raw.split("|")
    if not parts or parts[0] not in UNKNOWN_OUTCOME_CODES:
        return None
    if len(parts) > 1 + len(_DURABLE_REMOTE_KEYS):
        return None

    metadata: dict[str, str] = {}
    for token in parts[1:]:
        if "=" not in token:
            return None
        key, remote_value = token.split("=", 1)
        if key not in _DURABLE_REMOTE_KEYS or key in metadata:
            return None
        safe_value = sanitize_remote_code(remote_value)
        if safe_value is None or safe_value != remote_value:
            return None
        metadata[key] = safe_value

    canonical = build_durable_unknown_error(parts[0], metadata)
    if canonical != raw:
        return None
    return canonical


def request_shape(request: Any) -> dict[str, Any]:
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
    """Emite uma linha key=value contendo apenas campos explicitamente seguros."""
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

    for key in _DURABLE_REMOTE_KEYS:
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
    "record_remote_error_metadata",
    "consume_remote_error_metadata",
    "clear_remote_error_metadata",
    "record_unknown_outcome",
    "consume_unknown_outcome",
    "clear_unknown_outcome",
    "build_durable_unknown_error",
    "validate_durable_unknown_error",
    "request_shape",
    "log_placeorder_event",
]
