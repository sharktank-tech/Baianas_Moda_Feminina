"""Reconciliação offline de diagnósticos duráveis do placeorder — FASE 8.0.11T.

Este módulo NÃO executa HTTP, não lê credenciais e não toca catálogo. Ele só
classifica diagnósticos já persistidos que tenham passado pelo formato
fail-closed de ``placeorder_diagnostics.validate_durable_unknown_error``.

A regra é deliberadamente estreita: somente o par oficial TOP
``remote_error_code=29`` + ``remote_error_sub_code=isv.appkey-not-exists`` é
conclusivo para ``not_found``. Esse erro é uma rejeição de gateway por App Key
inválida/não reconhecida, portanto a chamada não chega à execução normal do
método de criação. Qualquer ausência, variação ou código desconhecido permanece
inconclusivo (``None``).
"""

from __future__ import annotations

from typing import Any, Optional

from .placeorder_diagnostics import validate_durable_unknown_error


DURABLE_RECONCILE_NOT_FOUND = "not_found"

_INVALID_APPKEY_CODE = "29"
_INVALID_APPKEY_SUB_CODE = "isv.appkey-not-exists"


def classify_durable_unknown_for_reconciliation(value: Any) -> Optional[str]:
    """Retorna um outcome conclusivo apenas para diagnósticos oficiais exatos.

    ``None`` significa: evidência insuficiente; manter ``unknown`` e o circuit
    breaker aberto. Nunca há heurística, substring matching ou fallback.
    """
    canonical = validate_durable_unknown_error(value)
    if canonical is None:
        return None

    parts = canonical.split("|")
    if not parts or parts[0] != "placeorder_remote_error":
        return None

    metadata: dict[str, str] = {}
    for token in parts[1:]:
        key, remote_value = token.split("=", 1)
        metadata[key] = remote_value

    if (
        metadata.get("remote_error_code") == _INVALID_APPKEY_CODE
        and metadata.get("remote_error_sub_code") == _INVALID_APPKEY_SUB_CODE
    ):
        return DURABLE_RECONCILE_NOT_FOUND

    return None


__all__ = [
    "DURABLE_RECONCILE_NOT_FOUND",
    "classify_durable_unknown_for_reconciliation",
]
