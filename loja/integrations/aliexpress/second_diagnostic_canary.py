"""Second diagnostic live-canary boundary — FASE 8.0.11K.

Este módulo NÃO altera nem reabre o first-canary da Fase 8.0.11I. Ele cria um
segundo slot explícito, exclusivamente diagnóstico, para o caso já reconciliado
como ``not_found`` após a primeira tentativa real ambígua.

Contrato de segurança:
- gate independente ``ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED == '1'``;
- o gate antigo ``ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED`` DEVE estar desligado;
- o controlled-canary + target allow-list continuam obrigatórios;
- ``ALIEXPRESS_PLACEORDER_ENABLED == '1'`` continua obrigatório e deve ser o
  último gate operacionalmente armado;
- exatamente UM durable attempt deve existir globalmente antes da execução;
- esse attempt anterior deve pertencer à mesma AliExpressOrder, estar
  ``rejected`` e ``completed_at`` preenchido;
- a ordem deve estar ``ready``, ``attempt_count == 1``, sem identidade remota,
  com snapshot imutável válido e submission gate válido;
- qualquer segundo durable attempt consome definitivamente este slot;
- ``in_flight``/``unknown`` continuam bloqueando pelo controlled-canary;
- nenhuma seleção automática de pedido; nenhuma rotação de credencial;
- nenhum token/payload/PII/remote order id é exposto pelo preflight.

A checagem crítica roda como ``pre_submit_guard`` dentro do mesmo advisory lock
global da Fase 8.0.11H, portanto duas execuções concorrentes não podem consumir
o slot ao mesmo tempo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from loja import db
from loja.modules.models import (
    AliExpressOrder,
    AliExpressRemoteOrder,
    AliExpressSubmissionAttempt,
)

from .controlled_canary import (
    get_canary_circuit_status,
    submit_controlled_canary_order,
)
from .placeorder_provider import PLACEORDER_ENABLED_ENV, get_real_order_submitter
from .single_live_canary import SINGLE_LIVE_CANARY_ENABLED_ENV
from .submission import (
    AliExpressOrderSubmitter,
    _preflight_submission_snapshot,
    _submission_gate,
)


SECOND_DIAGNOSTIC_CANARY_ENABLED_ENV = (
    "ALIEXPRESS_SECOND_DIAGNOSTIC_CANARY_ENABLED"
)
_CONTROLLED_CANARY_ENABLED_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ENABLED"
_CONTROLLED_CANARY_ORDER_ID_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID"


class AliExpressSecondDiagnosticCanaryConfigurationError(Exception):
    """O segundo canário diagnóstico não está armado de modo fail-closed."""


class AliExpressSecondDiagnosticCanaryConsumed(Exception):
    """O segundo slot já foi consumido por um durable attempt adicional."""


class AliExpressSecondDiagnosticCanaryPreflightError(Exception):
    """O candidato não satisfaz o estado local/reconciliação exigidos."""


@dataclass(frozen=True)
class SecondDiagnosticCanaryPreflight:
    """Preflight sanitizado, sem identidades sensíveis/remotas."""

    eligible: bool
    reason: str
    prior_attempt_count: int
    circuit_open: bool
    second_gate_enabled: bool
    first_gate_disabled: bool
    controlled_gate_enabled: bool
    target_matches: bool
    placeorder_enabled: bool


def _second_enabled() -> bool:
    return os.environ.get(SECOND_DIAGNOSTIC_CANARY_ENABLED_ENV) == "1"


def _first_disabled() -> bool:
    return os.environ.get(SINGLE_LIVE_CANARY_ENABLED_ENV) != "1"


def _controlled_enabled() -> bool:
    return os.environ.get(_CONTROLLED_CANARY_ENABLED_ENV) == "1"


def _target_matches(order_id) -> bool:
    configured = os.environ.get(_CONTROLLED_CANARY_ORDER_ID_ENV)
    return bool(configured) and configured.strip() == str(order_id)


def _placeorder_enabled() -> bool:
    return os.environ.get(PLACEORDER_ENABLED_ENV) == "1"


def _all_attempts(session) -> list[AliExpressSubmissionAttempt]:
    return (
        session.query(AliExpressSubmissionAttempt)
        .order_by(AliExpressSubmissionAttempt.created_at.asc())
        .all()
    )


def _remote_identity_exists(session, order_id) -> bool:
    return (
        session.query(AliExpressRemoteOrder.id)
        .filter(AliExpressRemoteOrder.aliexpress_order_id == order_id)
        .first()
        is not None
    )


def _candidate_reason(order: AliExpressOrder, *, session) -> str:
    """Retorna motivo estável e sanitizado, sem mutar estado."""

    attempts = _all_attempts(session)
    if len(attempts) == 0:
        return "second_diagnostic_canary_requires_prior_attempt"
    if len(attempts) > 1:
        return "second_diagnostic_canary_already_consumed"

    prior = attempts[0]
    if prior.aliexpress_order_id != order.id:
        return "second_diagnostic_canary_prior_attempt_order_mismatch"
    if prior.state != "rejected" or prior.completed_at is None:
        return "second_diagnostic_canary_prior_attempt_not_reconciled"

    circuit = get_canary_circuit_status(session=session)
    if circuit.open:
        return "submission_circuit_open"

    if order.state != "ready":
        return "second_diagnostic_canary_order_not_ready"
    if order.supplier_id is None:
        return "second_diagnostic_canary_requires_supplier"
    if int(order.attempt_count or 0) != 1:
        return "second_diagnostic_canary_attempt_count_mismatch"
    if order.ali_order_id is not None:
        return "second_diagnostic_canary_remote_identity_present"
    if _remote_identity_exists(session, order.id):
        return "second_diagnostic_canary_remote_identity_present"

    snapshot_error = _preflight_submission_snapshot(session, order.id)
    if snapshot_error is not None:
        return snapshot_error

    submission_error = _submission_gate(order, list(order.items), refresher=None)
    if submission_error is not None:
        return submission_error

    if not _second_enabled():
        return "second_diagnostic_canary_disabled"
    if not _first_disabled():
        return "single_live_canary_must_be_disabled"
    if not _controlled_enabled():
        return "controlled_canary_disabled"
    if not _target_matches(order.id):
        return "controlled_canary_target_mismatch"
    if not _placeorder_enabled():
        return "placeorder_disabled"

    return "ready_for_second_diagnostic_canary"


def get_second_diagnostic_canary_preflight(
    ali_order: AliExpressOrder,
    *,
    session=None,
) -> SecondDiagnosticCanaryPreflight:
    """Preflight somente leitura para um candidato explicitamente escolhido."""

    session = session or db.session
    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .populate_existing()
        .first()
    )
    attempts = _all_attempts(session)
    circuit_open = get_canary_circuit_status(session=session).open

    if order is None:
        return SecondDiagnosticCanaryPreflight(
            eligible=False,
            reason="aliexpress_order_not_found",
            prior_attempt_count=len(attempts),
            circuit_open=circuit_open,
            second_gate_enabled=_second_enabled(),
            first_gate_disabled=_first_disabled(),
            controlled_gate_enabled=_controlled_enabled(),
            target_matches=False,
            placeorder_enabled=_placeorder_enabled(),
        )

    reason = _candidate_reason(order, session=session)
    return SecondDiagnosticCanaryPreflight(
        eligible=reason == "ready_for_second_diagnostic_canary",
        reason=reason,
        prior_attempt_count=len(attempts),
        circuit_open=circuit_open,
        second_gate_enabled=_second_enabled(),
        first_gate_disabled=_first_disabled(),
        controlled_gate_enabled=_controlled_enabled(),
        target_matches=_target_matches(order.id),
        placeorder_enabled=_placeorder_enabled(),
    )


def _assert_second_diagnostic_candidate(
    order: AliExpressOrder,
    *,
    session,
) -> None:
    """Guard crítico chamado sob o advisory lock global."""

    reason = _candidate_reason(order, session=session)
    if reason == "ready_for_second_diagnostic_canary":
        return
    if reason == "second_diagnostic_canary_already_consumed":
        raise AliExpressSecondDiagnosticCanaryConsumed(reason)
    if reason in {
        "second_diagnostic_canary_disabled",
        "single_live_canary_must_be_disabled",
        "controlled_canary_disabled",
        "controlled_canary_target_mismatch",
        "placeorder_disabled",
    }:
        raise AliExpressSecondDiagnosticCanaryConfigurationError(reason)
    raise AliExpressSecondDiagnosticCanaryPreflightError(reason)


def submit_second_diagnostic_canary_order(
    ali_order: AliExpressOrder,
    *,
    submitter: Optional[AliExpressOrderSubmitter] = None,
    session=None,
):
    """Executa no máximo UM segundo attempt, quando explicitamente armado.

    ``submitter`` existe apenas para testes determinísticos. O caminho admin de
    produção deve omiti-lo e usar o TOP provider real explicitamente gated.
    """

    if not _second_enabled():
        raise AliExpressSecondDiagnosticCanaryConfigurationError(
            "second_diagnostic_canary_disabled"
        )
    if not _first_disabled():
        raise AliExpressSecondDiagnosticCanaryConfigurationError(
            "single_live_canary_must_be_disabled"
        )
    if not _placeorder_enabled():
        raise AliExpressSecondDiagnosticCanaryConfigurationError(
            "placeorder_disabled"
        )

    session = session or db.session
    resolved_submitter = submitter or get_real_order_submitter()

    return submit_controlled_canary_order(
        ali_order,
        submitter=resolved_submitter,
        session=session,
        pre_submit_guard=_assert_second_diagnostic_candidate,
    )


__all__ = [
    "SECOND_DIAGNOSTIC_CANARY_ENABLED_ENV",
    "AliExpressSecondDiagnosticCanaryConfigurationError",
    "AliExpressSecondDiagnosticCanaryConsumed",
    "AliExpressSecondDiagnosticCanaryPreflightError",
    "SecondDiagnosticCanaryPreflight",
    "get_second_diagnostic_canary_preflight",
    "submit_second_diagnostic_canary_order",
]
