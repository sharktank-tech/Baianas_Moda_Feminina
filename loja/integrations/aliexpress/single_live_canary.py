"""Single-live AliExpress canary boundary (Fase 8.0.11I).

This module is intentionally stricter than the generic controlled-canary
boundary from 8.0.11H.  It exists only for the FIRST real placeorder canary.

Safety contract:
* exact, independent ``ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED == '1'`` gate;
* exact H-level controlled-canary gate + target allow-list remain mandatory;
* exact ``ALIEXPRESS_PLACEORDER_ENABLED == '1'`` remains mandatory;
* ANY durable ``AliExpressSubmissionAttempt`` consumes the one-shot globally,
  regardless of whether it ended confirmed/rejected/unknown/in_flight;
* the one-shot check runs INSIDE the H PostgreSQL advisory-lock critical
  section, so two concurrent candidate orders cannot both pass;
* a candidate must still be pristine: ready, supplier-bound, attempt_count=0,
  no legacy/child remote identity, valid immutable submission snapshot and
  valid credential/item submission gate;
* no retry-by-age and no retry-after-ambiguous outcome;
* no token, credential, payload, attempt key or remote order id is exposed by
  status/errors from this module.

The module does not automatically choose a candidate and never changes Vercel
environment variables.  Operational arming/disarming remains an explicit
manual step described by the 8.0.11I runbook.
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
from .submission import (
    AliExpressOrderSubmitter,
    _preflight_submission_snapshot,
    _submission_gate,
)


SINGLE_LIVE_CANARY_ENABLED_ENV = "ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED"
_CONTROLLED_CANARY_ENABLED_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ENABLED"
_CONTROLLED_CANARY_ORDER_ID_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID"


class AliExpressSingleLiveCanaryConfigurationError(Exception):
    """The single-live canary is not explicitly armed."""


class AliExpressSingleLiveCanaryConsumed(Exception):
    """A durable submission attempt already consumed the one-shot."""


class AliExpressSingleLiveCanaryPreflightError(Exception):
    """The selected order failed a local fail-closed preflight."""


@dataclass(frozen=True)
class SingleLiveCanaryPreflight:
    """Sanitized preflight state for the admin UI/runbook."""

    eligible: bool
    reason: str
    one_shot_consumed: bool
    circuit_open: bool
    single_gate_enabled: bool
    controlled_gate_enabled: bool
    target_matches: bool
    placeorder_enabled: bool


def _single_live_enabled() -> bool:
    return os.environ.get(SINGLE_LIVE_CANARY_ENABLED_ENV) == "1"


def _controlled_enabled() -> bool:
    return os.environ.get(_CONTROLLED_CANARY_ENABLED_ENV) == "1"


def _target_matches(order_id) -> bool:
    configured = os.environ.get(_CONTROLLED_CANARY_ORDER_ID_ENV)
    return bool(configured) and configured.strip() == str(order_id)


def _placeorder_enabled() -> bool:
    return os.environ.get(PLACEORDER_ENABLED_ENV) == "1"


def _one_shot_consumed(session) -> bool:
    """Any durable attempt means the first live-canary slot is spent.

    This deliberately includes confirmed and rejected attempts.  A confirmed
    rejection is still a real provider attempt and must never silently turn
    the first-canary window into a retry loop.
    """

    return session.query(AliExpressSubmissionAttempt.id).first() is not None


def _remote_identity_exists(session, order_id) -> bool:
    return (
        session.query(AliExpressRemoteOrder.id)
        .filter(AliExpressRemoteOrder.aliexpress_order_id == order_id)
        .first()
        is not None
    )


def _candidate_reason(order: AliExpressOrder, *, session) -> str:
    """Return a stable sanitized reason; never mutates the order."""

    if _one_shot_consumed(session):
        return "single_live_canary_already_consumed"

    circuit = get_canary_circuit_status(session=session)
    if circuit.open:
        return "submission_circuit_open"

    if order.state != "ready":
        return "single_live_canary_order_not_ready"
    if order.supplier_id is None:
        return "single_live_canary_requires_supplier"
    if int(order.attempt_count or 0) != 0:
        return "single_live_canary_prior_attempt_count"
    if order.ali_order_id is not None:
        return "single_live_canary_remote_identity_present"
    if _remote_identity_exists(session, order.id):
        return "single_live_canary_remote_identity_present"

    snapshot_error = _preflight_submission_snapshot(session, order.id)
    if snapshot_error is not None:
        return snapshot_error

    submission_error = _submission_gate(order, list(order.items), refresher=None)
    if submission_error is not None:
        return submission_error

    if not _single_live_enabled():
        return "single_live_canary_disabled"
    if not _controlled_enabled():
        return "controlled_canary_disabled"
    if not _target_matches(order.id):
        return "controlled_canary_target_mismatch"
    if not _placeorder_enabled():
        return "placeorder_disabled"

    return "ready_for_single_live_canary"


def get_single_live_canary_preflight(
    ali_order: AliExpressOrder,
    *,
    session=None,
) -> SingleLiveCanaryPreflight:
    """Read-only sanitized preflight for one explicitly selected order."""

    session = session or db.session
    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .populate_existing()
        .first()
    )
    if order is None:
        return SingleLiveCanaryPreflight(
            eligible=False,
            reason="aliexpress_order_not_found",
            one_shot_consumed=_one_shot_consumed(session),
            circuit_open=get_canary_circuit_status(session=session).open,
            single_gate_enabled=_single_live_enabled(),
            controlled_gate_enabled=_controlled_enabled(),
            target_matches=False,
            placeorder_enabled=_placeorder_enabled(),
        )

    consumed = _one_shot_consumed(session)
    circuit_open = get_canary_circuit_status(session=session).open
    reason = _candidate_reason(order, session=session)
    return SingleLiveCanaryPreflight(
        eligible=reason == "ready_for_single_live_canary",
        reason=reason,
        one_shot_consumed=consumed,
        circuit_open=circuit_open,
        single_gate_enabled=_single_live_enabled(),
        controlled_gate_enabled=_controlled_enabled(),
        target_matches=_target_matches(order.id),
        placeorder_enabled=_placeorder_enabled(),
    )


def _assert_single_live_candidate(order: AliExpressOrder, *, session) -> None:
    """Critical-section guard executed while the H advisory lock is held."""

    reason = _candidate_reason(order, session=session)
    if reason == "ready_for_single_live_canary":
        return
    if reason == "single_live_canary_already_consumed":
        raise AliExpressSingleLiveCanaryConsumed(reason)
    if reason in {
        "single_live_canary_disabled",
        "controlled_canary_disabled",
        "controlled_canary_target_mismatch",
        "placeorder_disabled",
    }:
        raise AliExpressSingleLiveCanaryConfigurationError(reason)
    raise AliExpressSingleLiveCanaryPreflightError(reason)


def submit_single_live_canary_order(
    ali_order: AliExpressOrder,
    *,
    submitter: Optional[AliExpressOrderSubmitter] = None,
    session=None,
):
    """Execute the ONE allowed live-canary attempt through the H boundary.

    ``submitter`` is injectable only for deterministic tests.  Even with an
    injected fake, all three exact environment gates remain mandatory.  The
    production/admin path omits it and therefore obtains the explicitly gated
    real TOP submitter.
    """

    if not _single_live_enabled():
        raise AliExpressSingleLiveCanaryConfigurationError(
            "single_live_canary_disabled"
        )
    if not _placeorder_enabled():
        raise AliExpressSingleLiveCanaryConfigurationError("placeorder_disabled")

    session = session or db.session
    resolved_submitter = submitter or get_real_order_submitter()

    return submit_controlled_canary_order(
        ali_order,
        submitter=resolved_submitter,
        session=session,
        pre_submit_guard=_assert_single_live_candidate,
    )


__all__ = [
    "SINGLE_LIVE_CANARY_ENABLED_ENV",
    "AliExpressSingleLiveCanaryConfigurationError",
    "AliExpressSingleLiveCanaryConsumed",
    "AliExpressSingleLiveCanaryPreflightError",
    "SingleLiveCanaryPreflight",
    "get_single_live_canary_preflight",
    "submit_single_live_canary_order",
]
