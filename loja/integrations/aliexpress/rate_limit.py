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

from .errors import AliExpressError, AliExpressRateLimitError

#: Aviso exibido quando o bloqueio temporário persiste após as tentativas.
RATE_LIMIT_MESSAGE = (
    "Limite temporário do AliExpress. "
    "Aguarde alguns segundos e tente novamente."
)


class BudgetExhaustedError(AliExpressError):
    """Orçamento de tempo esgotado antes de concluir a rodada.

    NÃO é rate limit do fornecedor: indica que esperar ou chamar de novo
    estouraria o teto da operação. Carrega mensagem pronta para exibição
    (aviso temporário ou falta de tempo) e nunca dados fabricados. Classe
    separada de `AliExpressRateLimitError` de propósito: o helper a
    propaga de imediato, sem dormir nem repetir.
    """

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
#: "ban" cobre o formato do provider ("this ban will last N seconds").
_RETRY_INTENT = (
    "retry",
    "retries",
    "wait",
    "sleep",
    "later",
    "ban",
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


class TimeBudget:
    """Orçamento de tempo (relógio monotônico) compartilhado por chamadas.

    `clock` injetável para testes (padrão `time.monotonic`). `remaining()`
    nunca é negativo. O orçamento cobre chamadas de rede E esperas: quem
    consome (chamada ou sleep real) avança o relógio e reduz o restante
    para os demais — inclusive entre as duas consultas on/off da rodada.
    """

    def __init__(self, budget_seconds: float, *, clock=time.monotonic):
        self._clock = clock
        self._deadline = clock() + max(float(budget_seconds), 0.0)

    def remaining(self) -> float:
        """Segundos restantes (0.0 quando esgotado)."""
        return max(self._deadline - self._clock(), 0.0)


def call_with_rate_limit_retry(
    client,
    method: str,
    business: Mapping[str, Any],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY_SECONDS,
    max_delay: float = MAX_DELAY_SECONDS,
    sleep=time.sleep,
    budget: Optional["TimeBudget"] = None,
):
    """Executa `client.call` com retry limitado SÓ em Rate Limit.

    Cada tentativa reconstrói os parâmetros (timestamp/sign frescos).
    Qualquer outro erro (API, auth, transporte, config) propaga de imediato,
    sem nova tentativa. Esgotadas as tentativas, o último
    `AliExpressRateLimitError` é relançado para o aviso ao usuário.

    Com `budget`, nenhuma chamada começa se seu timeout (`client.timeout`)
    ultrapassar o restante, e nenhuma espera começa se não couber: nesses
    casos levanta `BudgetExhaustedError` de imediato, sem dormir nem chamar.
    Espera do fornecedor maior que o restante encerra com o aviso temporário
    (nunca reduzida para repetir antes do prazo).
    """
    call_timeout = getattr(client, "timeout", None)
    attempts = 0
    while True:
        if (
            budget is not None
            and call_timeout is not None
            and budget.remaining() < float(call_timeout)
        ):
            raise BudgetExhaustedError(
                "Consulta não iniciada por falta de tempo no orçamento."
            )
        try:
            return client.call(method, **dict(business))
        except AliExpressRateLimitError as exc:
            if attempts >= max(int(max_retries), 0):
                raise
            supplier = supplier_wait_seconds(exc)
            progressive = float(base_delay) * (2**attempts)
            delay = min(max(supplier or 0.0, progressive), float(max_delay))
            if budget is not None and delay > budget.remaining():
                raise BudgetExhaustedError(RATE_LIMIT_MESSAGE) from None
            sleep(max(delay, 0.0))
            attempts += 1


__all__ = [
    "BASE_DELAY_SECONDS",
    "MAX_DELAY_SECONDS",
    "MAX_RETRIES",
    "RATE_LIMIT_MESSAGE",
    "BudgetExhaustedError",
    "TimeBudget",
    "call_with_rate_limit_retry",
    "supplier_wait_seconds",
]
