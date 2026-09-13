"""FASE 8.1.0 — orquestrador one-shot de fulfillment AliExpress de produção.

Contrato desta camada:
1. avalia o preflight operacional read-only;
2. avalia o circuit breaker global read-only;
3. exige a dupla trava de produção/placeorder;
4. seleciona o submitter de produção;
5. chama ``submit_aliexpress_order`` NO MÁXIMO UMA VEZ por execução;
6. nunca faz retry, loop, backoff ou reconciliação implícita.

Um resultado ``pending_reconciliation`` é terminal para esta execução. O caller
precisa acionar o fluxo explícito de reconciliação antes de qualquer nova
submissão.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .production_circuit_breaker import (
    ProductionCircuitBreakerState,
    evaluate_production_circuit_breaker,
)
from .production_fulfillment import (
    get_production_order_submitter,
    production_fulfillment_gate_state,
)
from .production_preflight import (
    ProductionSubmissionEligibility,
    evaluate_production_submission_eligibility,
)
from .submission import FulfillmentResult, submit_aliexpress_order


ORCHESTRATOR_DISABLED = "production_fulfillment_disabled"
ORCHESTRATOR_DISPATCHED = "production_submission_dispatched"


@dataclass(frozen=True)
class ProductionOrchestrationResult:
    dispatched: bool
    code: Optional[str]
    state: Optional[str]
    eligible: bool


def orchestrate_production_submission(
    ali_order,
    *,
    session=None,
    refresher=None,
    preflight_fn: Callable[..., ProductionSubmissionEligibility] = (
        evaluate_production_submission_eligibility
    ),
    circuit_breaker_fn: Callable[..., ProductionCircuitBreakerState] = (
        evaluate_production_circuit_breaker
    ),
    gate_state_fn=production_fulfillment_gate_state,
    submitter_factory=get_production_order_submitter,
    submit_fn: Callable[..., FulfillmentResult] = submit_aliexpress_order,
) -> ProductionOrchestrationResult:
    """Executa no máximo uma submissão lógica, sempre precedida de guards."""
    eligibility = preflight_fn(
        ali_order,
        session=session,
        refresher=refresher,
    )
    if not eligibility.eligible:
        return ProductionOrchestrationResult(
            dispatched=False,
            code=eligibility.code,
            state=getattr(ali_order, "state", None),
            eligible=False,
        )

    circuit = circuit_breaker_fn(session=session)
    if circuit.open:
        return ProductionOrchestrationResult(
            dispatched=False,
            code=circuit.code,
            state=getattr(ali_order, "state", None),
            eligible=True,
        )

    gates = gate_state_fn()
    if not gates.can_select_real_provider:
        return ProductionOrchestrationResult(
            dispatched=False,
            code=ORCHESTRATOR_DISABLED,
            state=getattr(ali_order, "state", None),
            eligible=True,
        )

    submitter = submitter_factory()
    result = submit_fn(
        ali_order,
        submitter=submitter,
        refresher=refresher,
        session=session,
    )

    return ProductionOrchestrationResult(
        dispatched=True,
        code=ORCHESTRATOR_DISPATCHED,
        state=getattr(result, "state", None),
        eligible=True,
    )


__all__ = [
    "ORCHESTRATOR_DISABLED",
    "ORCHESTRATOR_DISPATCHED",
    "ProductionOrchestrationResult",
    "orchestrate_production_submission",
]
