"""Tentativas limitadas com espera progressiva para Rate Limit (só leitura).

Aplica-se SOMENTE a consultas idempotentes read-only (busca/detalhe do
catálogo). Caminhos de escrita (pedidos, pagamento, fulfillment) NUNCA
repetem chamadas: `AliExpressClient.call()` continua levantando de
imediato — o retry é opt-in por chamada, via `call_with_rate_limit_retry`.

Política: até `max_retries` novas tentativas (total 1 + max_retries
chamadas). A espera de cada tentativa é `max(tempo do fornecedor,
base * 2**tentativa)`, limitada a `max_delay`. O tempo do fornecedor vem
de campos explícitos do `error_response` (`retry_after` e variantes) ou,
na ausência deles, de menção explícita ("tente novamente em N segundos")
na mensagem — sempre limitado ao teto. Sem tempo informado, vale só a
progressão (base, 2x base, ...).
"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping, Optional

from .errors import AliExpressRateLimitError

#: Aviso exibido quando o bloqueio temporário persiste após as tentativas.
RATE_LIMIT_MESSAGE = (
    "Limite temporário do AliExpress. "
    "Aguarde alguns segundos e tente novamente."
)

MAX_RETRIES = 2
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 10.0

#: Campos numéricos explícitos de espera no `error_response`.
_WAIT_FIELDS = (
    "retry_after",
    "retryafter",
    "retry_after_seconds",
    "retry_in",
    "wait_seconds",
    "wait",
    "sleep_seconds",
    "sleep",
)

#: A menção de espera na mensagem só vale com intenção de nova tentativa.
_RETRY_INTENT = (
    "retry",
    "retries",
    "wait",
    "sleep",
    "later",
    "try again",
    "tente novamente",
    "aguarde",
    "nova tentativa",
)

#: Número + unidade de tempo ("30 seconds", "30s", "30 segundos").
_WAIT_NUMBER_RE = re.compile(
    r"(\d{1,3})\s*(?:seconds?|secs?|segundos?)\b|(\d{1,3})s\b"
)


def _as_seconds(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds == float("inf") or seconds < 0:
        return None
    return seconds


def supplier_wait_seconds(exc: BaseException) -> Optional[float]:
    """Segundos pedidos pelo fornecedor no erro, ou None se ausentes.

    Lê apenas o `error_response` sanitizado já anexado à exceção (nunca
    toca em credenciais). Sem tempo informado, retorna None e vale a
    progressão local.
    """
    raw = getattr(exc, "raw", None)
    error = raw.get("error_response") if isinstance(raw, Mapping) else None
    if not isinstance(error, Mapping):
        return None

    lowered = {
        str(key).lower(): value
        for key, value in error.items()
        if isinstance(key, str)
    }
    for name in _WAIT_FIELDS:
        if name in lowered:
            seconds = _as_seconds(lowered[name])
            if seconds is not None:
                return seconds

    texts = [
        error.get("sub_msg"),
        error.get("sub_message"),
        error.get("msg"),
    ]
    joined = " ".join(str(t) for t in texts if t).lower()
    if not any(marker in joined for marker in _RETRY_INTENT):
        return None
    match = _WAIT_NUMBER_RE.search(joined)
    if not match:
        return None
    return _as_seconds(match.group(1) or match.group(2))


def call_with_rate_limit_retry(
    client,
    method: str,
    business: Mapping[str, Any],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY_SECONDS,
    max_delay: float = MAX_DELAY_SECONDS,
    sleep=time.sleep,
):
    """Executa `client.call` com retry limitado SÓ em Rate Limit.

    Cada tentativa reconstrói os parâmetros (timestamp/sign frescos).
    Qualquer outro erro (API, auth, transporte, config) propaga de imediato,
    sem nova tentativa. Esgotadas as tentativas, o último
    `AliExpressRateLimitError` é relançado para o aviso ao usuário.
    """
    attempts = 0
    while True:
        try:
            return client.call(method, **dict(business))
        except AliExpressRateLimitError as exc:
            if attempts >= max(int(max_retries), 0):
                raise
            supplier = supplier_wait_seconds(exc)
            progressive = float(base_delay) * (2**attempts)
            delay = min(max(supplier or 0.0, progressive), float(max_delay))
            sleep(max(delay, 0.0))
            attempts += 1


__all__ = [
    "BASE_DELAY_SECONDS",
    "MAX_DELAY_SECONDS",
    "MAX_RETRIES",
    "RATE_LIMIT_MESSAGE",
    "call_with_rate_limit_retry",
    "supplier_wait_seconds",
]
