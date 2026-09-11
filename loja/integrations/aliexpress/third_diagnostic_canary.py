"""Third diagnostic live-canary boundary — FASE 8.0.11O.

Este módulo NÃO autoriza uma terceira chamada real por si só. Ele instala um
novo boundary one-shot, desligado por padrão, para uma eventual terceira
investigação explicitamente autorizada depois de DUAS tentativas anteriores
reconciliadas como ``not_found``/``rejected``.

Contrato de segurança:
- gate novo e independente ``ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED == '1'``;
- os gates do first e second canary DEVEM estar desligados;
- controlled-canary + target exato continuam obrigatórios;
- ``ALIEXPRESS_PLACEORDER_ENABLED == '1'`` continua obrigatório e deve ser o
  último gate operacionalmente armado;
- exatamente DOIS durable attempts devem existir globalmente antes da execução;
- ambos devem pertencer à mesma AliExpressOrder, estar ``rejected`` e com
  ``completed_at`` preenchido;
- a ordem deve estar ``ready``, ``attempt_count == 2``, sem identidade remota,
  com snapshot imutável válido e submission gate válido;
- qualquer terceiro durable attempt consome definitivamente este slot;
- ``in_flight``/``unknown`` continuam bloqueando pelo controlled-canary;
- nenhuma seleção automática de pedido, retry ou rotação de credencial;
- nenhum token/payload/PII/remote order id é exposto pelo preflight.

A checagem crítica roda como ``pre_submit_guard`` dentro do mesmo advisory lock
global do controlled canary. Portanto, concorrência não cria dois terceiros
attempts. Deploy/GET/preflight não fazem HTTP ao AliExpress.
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
from .second_diagnostic_canary import SECOND_DIAGNOSTIC_CANARY_ENABLED_ENV
from .single_live_canary import SINGLE_LIVE_CANARY_ENABLED_ENV
from .submission import (
    AliExpressOrderSubmitter,
    _preflight_submission_snapshot,
    _submission_gate,
)


THIRD_DIAGNOSTIC_CANARY_ENABLED_ENV = "ALIEXPRESS_THIRD_DIAGNOSTIC_CANARY_ENABLED"
_CONTROLLED_CANARY_ENABLED_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ENABLED"
_CONTROLLED_CANARY_ORDER_ID_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID"


class AliExpressThirdDiagnosticCanaryConfigurationError(Exception):
    """O terceiro canário diagnóstico não está armado de modo fail-closed."""


class AliExpressThirdDiagnosticCanaryConsumed(Exception):
    """O terceiro slot já foi consumido por um durable attempt adicional."""


class AliExpressThirdDiagnosticCanaryPreflightError(Exception):
    """O candidato não satisfaz o estado local/reconciliação exigidos."""


@dataclass(frozen=True)
class ThirdDiagnosticCanaryPreflight:
    """Preflight sanitizado, sem identidades sensíveis/remotas."""

    eligible: bool
    reason: str
    prior_attempt_count: int
    circuit_open: bool
    third_gate_enabled: bool
    first_gate_disabled: bool
    second_gate_disabled: bool
    controlled_gate_enabled: bool
    target_matches: bool
    placeorder_enabled: bool


def _third_enabled() -> bool:
    return os.environ.get(THIRD_DIAGNOSTIC_CANARY_ENABLED_ENV) == "1"


def _first_disabled() -> bool:
    return os.environ.get(SINGLE_LIVE_CANARY_ENABLED_ENV) != "1"


def _second_disabled() -> bool:
    return os.environ.get(SECOND_DIAGNOSTIC_CANARY_ENABLED_ENV) != "1"


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
    if len(attempts) < 2:
        return "third_diagnostic_canary_requires_two_prior_attempts"
    if len(attempts) > 2:
        return "third_diagnostic_canary_already_consumed"

    if any(attempt.aliexpress_order_id != order.id for attempt in attempts):
        return "third_diagnostic_canary_prior_attempt_order_mismatch"
    if any(
        attempt.state != "rejected" or attempt.completed_at is None
        for attempt in attempts
    ):
        return "third_diagnostic_canary_prior_attempts_not_reconciled"

    circuit = get_canary_circuit_status(session=session)
    if circuit.open:
        return "submission_circuit_open"

    if order.state != "ready":
        return "third_diagnostic_canary_order_not_ready"
    if order.supplier_id is None:
        return "third_diagnostic_canary_requires_supplier"
    if int(order.attempt_count or 0) != 2:
        return "third_diagnostic_canary_attempt_count_mismatch"
    if order.ali_order_id is not None:
        return "third_diagnostic_canary_remote_identity_present"
    if _remote_identity_exists(session, order.id):
        return "third_diagnostic_canary_remote_identity_present"

    snapshot_error = _preflight_submission_snapshot(session, order.id)
    if snapshot_error is not None:
        return snapshot_error

    submission_error = _submission_gate(order, list(order.items), refresher=None)
    if submission_error is not None:
        return submission_error

    if not _third_enabled():
        return "third_diagnostic_canary_disabled"
    if not _first_disabled():
        return "single_live_canary_must_be_disabled"
    if not _second_disabled():
        return "second_diagnostic_canary_must_be_disabled"
    if not _controlled_enabled():
        return "controlled_canary_disabled"
    if not _target_matches(order.id):
        return "controlled_canary_target_mismatch"
    if not _placeorder_enabled():
        return "placeorder_disabled"

    return "ready_for_third_diagnostic_canary"


def get_third_diagnostic_canary_preflight(
    ali_order: AliExpressOrder,
    *,
    session=None,
) -> ThirdDiagnosticCanaryPreflight:
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
        return ThirdDiagnosticCanaryPreflight(
            eligible=False,
            reason="aliexpress_order_not_found",
            prior_attempt_count=len(attempts),
            circuit_open=circuit_open,
            third_gate_enabled=_third_enabled(),
            first_gate_disabled=_first_disabled(),
            second_gate_disabled=_second_disabled(),
            controlled_gate_enabled=_controlled_enabled(),
            target_matches=False,
            placeorder_enabled=_placeorder_enabled(),
        )

    reason = _candidate_reason(order, session=session)
    return ThirdDiagnosticCanaryPreflight(
        eligible=reason == "ready_for_third_diagnostic_canary",
        reason=reason,
        prior_attempt_count=len(attempts),
        circuit_open=circuit_open,
        third_gate_enabled=_third_enabled(),
        first_gate_disabled=_first_disabled(),
        second_gate_disabled=_second_disabled(),
        controlled_gate_enabled=_controlled_enabled(),
        target_matches=_target_matches(order.id),
        placeorder_enabled=_placeorder_enabled(),
    )


def _assert_third_diagnostic_candidate(
    order: AliExpressOrder,
    *,
    session,
) -> None:
    """Guard crítico chamado sob o advisory lock global."""

    reason = _candidate_reason(order, session=session)
    if reason == "ready_for_third_diagnostic_canary":
        return
    if reason == "third_diagnostic_canary_already_consumed":
        raise AliExpressThirdDiagnosticCanaryConsumed(reason)
    if reason in {
        "third_diagnostic_canary_disabled",
        "single_live_canary_must_be_disabled",
        "second_diagnostic_canary_must_be_disabled",
        "controlled_canary_disabled",
        "controlled_canary_target_mismatch",
        "placeorder_disabled",
    }:
        raise AliExpressThirdDiagnosticCanaryConfigurationError(reason)
    raise AliExpressThirdDiagnosticCanaryPreflightError(reason)


def submit_third_diagnostic_canary_order(
    ali_order: AliExpressOrder,
    *,
    submitter: Optional[AliExpressOrderSubmitter] = None,
    session=None,
):
    """Executa no máximo UM terceiro attempt quando houver autorização externa.

    ``submitter`` existe apenas para testes determinísticos. O caminho admin de
    produção deve omiti-lo. Este módulo não arma variáveis de ambiente e não
    transforma um simples deploy em autorização para compra real.
    """

    if not _third_enabled():
        raise AliExpressThirdDiagnosticCanaryConfigurationError(
            "third_diagnostic_canary_disabled"
        )
    if not _first_disabled():
        raise AliExpressThirdDiagnosticCanaryConfigurationError(
            "single_live_canary_must_be_disabled"
        )
    if not _second_disabled():
        raise AliExpressThirdDiagnosticCanaryConfigurationError(
            "second_diagnostic_canary_must_be_disabled"
        )
    if not _placeorder_enabled():
        raise AliExpressThirdDiagnosticCanaryConfigurationError(
            "placeorder_disabled"
        )

    session = session or db.session
    resolved_submitter = submitter or get_real_order_submitter()

    return submit_controlled_canary_order(
        ali_order,
        submitter=resolved_submitter,
        session=session,
        pre_submit_guard=_assert_third_diagnostic_candidate,
    )


__all__ = [
    "THIRD_DIAGNOSTIC_CANARY_ENABLED_ENV",
    "AliExpressThirdDiagnosticCanaryConfigurationError",
    "AliExpressThirdDiagnosticCanaryConsumed",
    "AliExpressThirdDiagnosticCanaryPreflightError",
    "ThirdDiagnosticCanaryPreflight",
    "get_third_diagnostic_canary_preflight",
    "submit_third_diagnostic_canary_order",
]
