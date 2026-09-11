"""Reconciliação offline de diagnósticos duráveis do placeorder — FASE 8.0.11T/8.0.11W.

Este módulo NÃO executa HTTP, não lê credenciais e não toca catálogo. Ele só
classifica diagnósticos já persistidos que tenham passado pelo formato
fail-closed de ``placeorder_diagnostics.validate_durable_unknown_error``.

As regras são deliberadamente estreitas e exatas:

* ``remote_error_code=29`` + ``remote_error_sub_code=isv.appkey-not-exists``;
* ``remote_error_code=B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL``.

Ambos representam rejeições conclusivas sem identidade remota confirmada para
o fluxo controlado. Qualquer ausência, variação ou código desconhecido permanece
inconclusivo (``None``). Não há heurística, substring matching ou fallback.
"""

from __future__ import annotations

from typing import Any, Optional

from .placeorder_diagnostics import validate_durable_unknown_error


DURABLE_RECONCILE_NOT_FOUND = "not_found"

_INVALID_APPKEY_CODE = "29"
_INVALID_APPKEY_SUB_CODE = "isv.appkey-not-exists"
_DELIVERY_ADDRESS_VALIDATE_FAIL_CODE = "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"


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

    if (
        metadata.get("remote_error_code")
        == _DELIVERY_ADDRESS_VALIDATE_FAIL_CODE
        and "remote_error_sub_code" not in metadata
    ):
        return DURABLE_RECONCILE_NOT_FOUND

    return None


__all__ = [
    "DURABLE_RECONCILE_NOT_FOUND",
    "classify_durable_unknown_for_reconciliation",
]
