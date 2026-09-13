"""FASE 8.1.0 — snapshot read-only de observabilidade operacional.

Expõe apenas booleanos/códigos sanitizados e contagens agregadas. Não retorna
valores crus de env, PII, tokens, attempt keys ou identidades remotas.
"""

from __future__ import annotations

from dataclasses import dataclass

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt

from .postpayment_autodispatch import postpayment_autodispatch_enabled
from .production_circuit_breaker import evaluate_production_circuit_breaker
from .production_fulfillment import production_fulfillment_gate_state


@dataclass(frozen=True)
class ProductionObservabilitySnapshot:
    postpayment_autodispatch_enabled: bool
    production_fulfillment_enabled: bool
    placeorder_enabled: bool
    all_submission_gates_enabled: bool
    circuit_open: bool
    circuit_code: str | None
    ready_count: int
    blocked_count: int
    pending_reconciliation_count: int
    submitting_count: int
    submitted_count: int
    unresolved_attempt_count: int


def get_production_observability_snapshot(*, session=None) -> ProductionObservabilitySnapshot:
    session = session or db.session
    gates = production_fulfillment_gate_state()
    autodispatch = postpayment_autodispatch_enabled()
    circuit = evaluate_production_circuit_breaker(session=session)

    def order_count(state: str) -> int:
        return int(
            session.query(AliExpressOrder.id)
            .filter(AliExpressOrder.state == state)
            .count()
            or 0
        )

    unresolved_attempt_count = int(
        session.query(AliExpressSubmissionAttempt.id)
        .filter(
            AliExpressSubmissionAttempt.state.in_(("in_flight", "unknown")),
            AliExpressSubmissionAttempt.completed_at.is_(None),
        )
        .count()
        or 0
    )

    return ProductionObservabilitySnapshot(
        postpayment_autodispatch_enabled=autodispatch,
        production_fulfillment_enabled=gates.production_enabled,
        placeorder_enabled=gates.placeorder_enabled,
        all_submission_gates_enabled=(
            autodispatch and gates.can_select_real_provider
        ),
        circuit_open=circuit.open,
        circuit_code=circuit.code,
        ready_count=order_count("ready"),
        blocked_count=order_count("blocked"),
        pending_reconciliation_count=order_count("pending_reconciliation"),
        submitting_count=order_count("submitting"),
        submitted_count=order_count("submitted"),
        unresolved_attempt_count=unresolved_attempt_count,
    )


__all__ = [
    "ProductionObservabilitySnapshot",
    "get_production_observability_snapshot",
]
