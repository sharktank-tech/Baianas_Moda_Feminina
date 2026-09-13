"""FASE 8.1.0 — orquestrador one-shot de fulfillment AliExpress de produção.

Contrato desta camada:
1. avalia o preflight operacional read-only;
2. exige a dupla trava de produção/placeorder;
3. seleciona o submitter de produção;
4. chama ``submit_aliexpress_order`` NO MÁXIMO UMA VEZ por execução;
5. nunca faz retry, loop, backoff ou reconciliação implícita.

Um resultado ``pending_reconciliation`` é terminal para esta execução. O caller
precisa acionar o fluxo explícito de reconciliação antes de qualquer nova
submissão. O orquestrador não é conectado automaticamente ao webhook/pagamento
neste checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

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
    gate_state_fn=production_fulfillment_gate_state,
    submitter_factory=get_production_order_submitter,
    submit_fn: Callable[..., FulfillmentResult] = submit_aliexpress_order,
) -> ProductionOrchestrationResult:
    """Executa no máximo uma submissão lógica, sempre precedida de preflight.

    Dependências são injetáveis somente para teste. O caminho padrão usa os
    boundaries canônicos de produção. Nenhum erro retornado inclui payload,
    credencial, CPF, attempt key ou remote order id.
    """
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

    # Não há qualquer decisão de retry aqui. submitted, submitting,
    # pending_reconciliation ou outro estado retornado é repassado ao caller.
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
