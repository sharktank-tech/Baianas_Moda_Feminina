"""Fourth diagnostic live-canary boundary — FASE 8.0.11V.

This boundary authorizes no HTTP by itself. It creates a new one-shot slot for
exactly one explicitly authorized FOURTH real AliExpress placeorder attempt
after three prior attempts have been conclusively rejected.

Safety contract:
- new independent gate ``ALIEXPRESS_FOURTH_DIAGNOSTIC_CANARY_ENABLED == '1'``;
- first, second, third and third-final-execution gates MUST be OFF;
- controlled-canary + exact target remain mandatory;
- ``ALIEXPRESS_PLACEORDER_ENABLED == '1'`` remains mandatory;
- exactly THREE durable attempts must exist globally before execution;
- all three must belong to the same AliExpressOrder, be ``rejected`` and have
  ``completed_at`` set;
- order must be ``ready``, ``attempt_count == 3``, have no remote identity,
  and pass immutable snapshot/submission guards;
- any fourth durable attempt permanently consumes this slot;
- in-flight/unknown attempts keep the global circuit open;
- no retry, automatic order selection, credential rotation, payload logging or
  remote-id exposure is introduced here.
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
from .third_diagnostic_canary import THIRD_DIAGNOSTIC_CANARY_ENABLED_ENV
from .third_diagnostic_execution import THIRD_DIAGNOSTIC_EXECUTION_ENABLED_ENV


FOURTH_DIAGNOSTIC_CANARY_ENABLED_ENV = "ALIEXPRESS_FOURTH_DIAGNOSTIC_CANARY_ENABLED"
_CONTROLLED_CANARY_ENABLED_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ENABLED"
_CONTROLLED_CANARY_ORDER_ID_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID"


class AliExpressFourthDiagnosticCanaryConfigurationError(Exception):
    """The fourth canary is not safely armed."""


class AliExpressFourthDiagnosticCanaryConsumed(Exception):
    """A fourth durable attempt already consumed this one-shot slot."""


class AliExpressFourthDiagnosticCanaryPreflightError(Exception):
    """Local durable state does not satisfy fourth-canary requirements."""


@dataclass(frozen=True)
class FourthDiagnosticCanaryPreflight:
    eligible: bool
    reason: str
    prior_attempt_count: int
    circuit_open: bool
    fourth_gate_enabled: bool
    prior_gates_disabled: bool
    controlled_gate_enabled: bool
    target_matches: bool
    placeorder_enabled: bool


def _fourth_enabled() -> bool:
    return os.environ.get(FOURTH_DIAGNOSTIC_CANARY_ENABLED_ENV) == "1"


def _prior_gates_disabled() -> bool:
    return all(
        os.environ.get(name) != "1"
        for name in (
            SINGLE_LIVE_CANARY_ENABLED_ENV,
            SECOND_DIAGNOSTIC_CANARY_ENABLED_ENV,
            THIRD_DIAGNOSTIC_CANARY_ENABLED_ENV,
            THIRD_DIAGNOSTIC_EXECUTION_ENABLED_ENV,
        )
    )


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
    attempts = _all_attempts(session)
    if len(attempts) < 3:
        return "fourth_diagnostic_canary_requires_three_prior_attempts"
    if len(attempts) > 3:
        return "fourth_diagnostic_canary_already_consumed"

    if any(attempt.aliexpress_order_id != order.id for attempt in attempts):
        return "fourth_diagnostic_canary_prior_attempt_order_mismatch"
    if any(
        attempt.state != "rejected" or attempt.completed_at is None
        for attempt in attempts
    ):
        return "fourth_diagnostic_canary_prior_attempts_not_reconciled"

    circuit = get_canary_circuit_status(session=session)
    if circuit.open:
        return "submission_circuit_open"

    if order.state != "ready":
        return "fourth_diagnostic_canary_order_not_ready"
    if order.supplier_id is None:
        return "fourth_diagnostic_canary_requires_supplier"
    if int(order.attempt_count or 0) != 3:
        return "fourth_diagnostic_canary_attempt_count_mismatch"
    if order.ali_order_id is not None or _remote_identity_exists(session, order.id):
        return "fourth_diagnostic_canary_remote_identity_present"

    snapshot_error = _preflight_submission_snapshot(session, order.id)
    if snapshot_error is not None:
        return snapshot_error

    submission_error = _submission_gate(order, list(order.items), refresher=None)
    if submission_error is not None:
        return submission_error

    if not _fourth_enabled():
        return "fourth_diagnostic_canary_disabled"
    if not _prior_gates_disabled():
        return "prior_canary_gates_must_be_disabled"
    if not _controlled_enabled():
        return "controlled_canary_disabled"
    if not _target_matches(order.id):
        return "controlled_canary_target_mismatch"
    if not _placeorder_enabled():
        return "placeorder_disabled"

    return "ready_for_fourth_diagnostic_canary"


def get_fourth_diagnostic_canary_preflight(
    ali_order: AliExpressOrder,
    *,
    session=None,
) -> FourthDiagnosticCanaryPreflight:
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
        return FourthDiagnosticCanaryPreflight(
            eligible=False,
            reason="aliexpress_order_not_found",
            prior_attempt_count=len(attempts),
            circuit_open=circuit_open,
            fourth_gate_enabled=_fourth_enabled(),
            prior_gates_disabled=_prior_gates_disabled(),
            controlled_gate_enabled=_controlled_enabled(),
            target_matches=False,
            placeorder_enabled=_placeorder_enabled(),
        )

    reason = _candidate_reason(order, session=session)
    return FourthDiagnosticCanaryPreflight(
        eligible=reason == "ready_for_fourth_diagnostic_canary",
        reason=reason,
        prior_attempt_count=len(attempts),
        circuit_open=circuit_open,
        fourth_gate_enabled=_fourth_enabled(),
        prior_gates_disabled=_prior_gates_disabled(),
        controlled_gate_enabled=_controlled_enabled(),
        target_matches=_target_matches(order.id),
        placeorder_enabled=_placeorder_enabled(),
    )


def _assert_fourth_diagnostic_candidate(order: AliExpressOrder, *, session) -> None:
    reason = _candidate_reason(order, session=session)
    if reason == "ready_for_fourth_diagnostic_canary":
        return
    if reason == "fourth_diagnostic_canary_already_consumed":
        raise AliExpressFourthDiagnosticCanaryConsumed(reason)
    if reason in {
        "fourth_diagnostic_canary_disabled",
        "prior_canary_gates_must_be_disabled",
        "controlled_canary_disabled",
        "controlled_canary_target_mismatch",
        "placeorder_disabled",
    }:
        raise AliExpressFourthDiagnosticCanaryConfigurationError(reason)
    raise AliExpressFourthDiagnosticCanaryPreflightError(reason)


def submit_fourth_diagnostic_canary_order(
    ali_order: AliExpressOrder,
    *,
    submitter: Optional[AliExpressOrderSubmitter] = None,
    session=None,
):
    """Execute at most one FOURTH durable attempt when externally authorized."""

    if not _fourth_enabled():
        raise AliExpressFourthDiagnosticCanaryConfigurationError(
            "fourth_diagnostic_canary_disabled"
        )
    if not _prior_gates_disabled():
        raise AliExpressFourthDiagnosticCanaryConfigurationError(
            "prior_canary_gates_must_be_disabled"
        )
    if not _placeorder_enabled():
        raise AliExpressFourthDiagnosticCanaryConfigurationError("placeorder_disabled")

    session = session or db.session
    resolved_submitter = submitter or get_real_order_submitter()
    return submit_controlled_canary_order(
        ali_order,
        submitter=resolved_submitter,
        session=session,
        pre_submit_guard=_assert_fourth_diagnostic_candidate,
    )


__all__ = [
    "FOURTH_DIAGNOSTIC_CANARY_ENABLED_ENV",
    "AliExpressFourthDiagnosticCanaryConfigurationError",
    "AliExpressFourthDiagnosticCanaryConsumed",
    "AliExpressFourthDiagnosticCanaryPreflightError",
    "FourthDiagnosticCanaryPreflight",
    "get_fourth_diagnostic_canary_preflight",
    "submit_fourth_diagnostic_canary_order",
]
