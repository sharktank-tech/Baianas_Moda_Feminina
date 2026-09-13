"""FASE 8.1.0 — circuit breaker read-only para submissões AliExpress.

O breaker protege o pipeline automático antes da seleção do provider. Ele não
muta pedidos, attempts ou configuração e nunca chama rede.

Abre o circuito quando:
* existe qualquer AliExpressOrder em ``pending_reconciliation``;
* existe attempt ``in_flight``/``unknown`` sem ``completed_at``;
* as N tentativas concluídas mais recentes dentro da janela configurada são
  todas ``rejected``.

A janela/limiar são apenas controles operacionais. Configuração inválida falha
fechado. Códigos retornados são fixos e não carregam PII, attempt keys ou IDs
remotos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt
from loja.utils import utcnow


CIRCUIT_OPEN_RECONCILIATION = "production_circuit_open_reconciliation"
CIRCUIT_OPEN_UNRESOLVED_ATTEMPT = "production_circuit_open_unresolved_attempt"
CIRCUIT_OPEN_REJECTIONS = "production_circuit_open_rejections"
CIRCUIT_OPEN_CONFIG_INVALID = "production_circuit_open_config_invalid"

REJECTION_THRESHOLD_ENV = "ALIEXPRESS_PRODUCTION_CIRCUIT_BREAKER_REJECTION_THRESHOLD"
WINDOW_MINUTES_ENV = "ALIEXPRESS_PRODUCTION_CIRCUIT_BREAKER_WINDOW_MINUTES"
DEFAULT_REJECTION_THRESHOLD = 3
DEFAULT_WINDOW_MINUTES = 15
MAX_REJECTION_THRESHOLD = 20
MAX_WINDOW_MINUTES = 1440


@dataclass(frozen=True)
class ProductionCircuitBreakerState:
    open: bool
    code: Optional[str]
    rejection_threshold: int
    window_minutes: int
    recent_rejections: int = 0


def _bounded_int_env(name: str, default: int, maximum: int) -> tuple[int, bool]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default, True
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, False
    if value < 1 or value > maximum:
        return default, False
    return value, True


def evaluate_production_circuit_breaker(*, session=None) -> ProductionCircuitBreakerState:
    """Avalia o estado global do breaker sem qualquer mutação."""
    session = session or db.session

    threshold, threshold_valid = _bounded_int_env(
        REJECTION_THRESHOLD_ENV,
        DEFAULT_REJECTION_THRESHOLD,
        MAX_REJECTION_THRESHOLD,
    )
    window_minutes, window_valid = _bounded_int_env(
        WINDOW_MINUTES_ENV,
        DEFAULT_WINDOW_MINUTES,
        MAX_WINDOW_MINUTES,
    )

    if not threshold_valid or not window_valid:
        return ProductionCircuitBreakerState(
            open=True,
            code=CIRCUIT_OPEN_CONFIG_INVALID,
            rejection_threshold=threshold,
            window_minutes=window_minutes,
        )

    pending_reconciliation = (
        session.query(AliExpressOrder.id)
        .filter(AliExpressOrder.state == "pending_reconciliation")
        .first()
    )
    if pending_reconciliation is not None:
        return ProductionCircuitBreakerState(
            open=True,
            code=CIRCUIT_OPEN_RECONCILIATION,
            rejection_threshold=threshold,
            window_minutes=window_minutes,
        )

    unresolved_attempt = (
        session.query(AliExpressSubmissionAttempt.id)
        .filter(
            AliExpressSubmissionAttempt.state.in_(("in_flight", "unknown")),
            AliExpressSubmissionAttempt.completed_at.is_(None),
        )
        .first()
    )
    if unresolved_attempt is not None:
        return ProductionCircuitBreakerState(
            open=True,
            code=CIRCUIT_OPEN_UNRESOLVED_ATTEMPT,
            rejection_threshold=threshold,
            window_minutes=window_minutes,
        )

    cutoff = utcnow() - timedelta(minutes=window_minutes)
    recent_states = [
        row[0]
        for row in (
            session.query(AliExpressSubmissionAttempt.state)
            .filter(
                AliExpressSubmissionAttempt.completed_at.isnot(None),
                AliExpressSubmissionAttempt.completed_at >= cutoff,
            )
            .order_by(
                AliExpressSubmissionAttempt.completed_at.desc(),
                AliExpressSubmissionAttempt.started_at.desc(),
            )
            .limit(threshold)
            .all()
        )
    ]

    recent_rejections = sum(1 for state in recent_states if state == "rejected")
    if len(recent_states) == threshold and recent_rejections == threshold:
        return ProductionCircuitBreakerState(
            open=True,
            code=CIRCUIT_OPEN_REJECTIONS,
            rejection_threshold=threshold,
            window_minutes=window_minutes,
            recent_rejections=recent_rejections,
        )

    return ProductionCircuitBreakerState(
        open=False,
        code=None,
        rejection_threshold=threshold,
        window_minutes=window_minutes,
        recent_rejections=recent_rejections,
    )


__all__ = [
    "CIRCUIT_OPEN_RECONCILIATION",
    "CIRCUIT_OPEN_UNRESOLVED_ATTEMPT",
    "CIRCUIT_OPEN_REJECTIONS",
    "CIRCUIT_OPEN_CONFIG_INVALID",
    "REJECTION_THRESHOLD_ENV",
    "WINDOW_MINUTES_ENV",
    "ProductionCircuitBreakerState",
    "evaluate_production_circuit_breaker",
]
