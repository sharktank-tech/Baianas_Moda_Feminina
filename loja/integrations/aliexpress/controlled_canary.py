"""Controlled live-canary guard for AliExpress placeorder (Fase 8.0.11H/8.0.11J).

This module does NOT enable AliExpress HTTP and does not select the real
provider. It is an additional fail-closed boundary that must wrap every future
controlled live submission.

FASE 8.0.11J preserves a sanitized provider outcome code on an ``unknown``
submission attempt after the core commits. The parent AliExpressOrder keeps the
canonical public ``submission_unknown`` error; no payload, credential, remote
ID or PII is persisted by this diagnostic layer.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import text

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt
from loja.utils import utcnow

from .fulfillment import FulfillmentResult
from .placeorder_diagnostics import clear_unknown_outcome, consume_unknown_outcome
from .submission import AliExpressOrderSubmitter, submit_aliexpress_order


_CANARY_ADVISORY_LOCK_KEY = 80110110011
_BLOCKING_ATTEMPT_STATES = ("in_flight", "unknown")
_CANARY_ENABLED_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ENABLED"
_CANARY_ORDER_ID_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID"


class AliExpressCanaryCircuitOpen(Exception):
    """A prior ambiguous/in-flight attempt blocks a new live canary."""


class AliExpressCanaryConfigurationError(Exception):
    """Controlled canary is not safely configured."""


@dataclass(frozen=True)
class CanaryCircuitStatus:
    """Sanitized operational status; never exposes attempt/order identities."""

    open: bool
    blocking_count: int
    has_in_flight: bool
    has_unknown: bool


def _canary_enabled() -> bool:
    return os.environ.get(_CANARY_ENABLED_ENV) == "1"


def _canary_target_matches(order_id) -> bool:
    configured = os.environ.get(_CANARY_ORDER_ID_ENV)
    if not configured:
        return False
    return configured.strip() == str(order_id)


def _assert_canary_armed_for(order_id) -> None:
    if not _canary_enabled():
        raise AliExpressCanaryConfigurationError("controlled_canary_disabled")
    if not _canary_target_matches(order_id):
        raise AliExpressCanaryConfigurationError("controlled_canary_target_mismatch")


@contextmanager
def _global_canary_guard(session):
    bind = session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        yield
        return

    connection = bind.connect()
    locked = False
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": _CANARY_ADVISORY_LOCK_KEY},
        )
        locked = True
        yield
    finally:
        if locked:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _CANARY_ADVISORY_LOCK_KEY},
                )
            finally:
                connection.close()
        else:
            connection.close()


def get_canary_circuit_status(
    *,
    session=None,
    exclude_order_id=None,
) -> CanaryCircuitStatus:
    session = session or db.session
    query = session.query(AliExpressSubmissionAttempt).filter(
        AliExpressSubmissionAttempt.state.in_(_BLOCKING_ATTEMPT_STATES)
    )
    if exclude_order_id is not None:
        query = query.filter(
            AliExpressSubmissionAttempt.aliexpress_order_id != exclude_order_id
        )

    blocking_count = query.count()
    if blocking_count == 0:
        return CanaryCircuitStatus(
            open=False,
            blocking_count=0,
            has_in_flight=False,
            has_unknown=False,
        )

    has_in_flight = (
        query.filter(AliExpressSubmissionAttempt.state == "in_flight").first()
        is not None
    )
    has_unknown = (
        query.filter(AliExpressSubmissionAttempt.state == "unknown").first()
        is not None
    )
    return CanaryCircuitStatus(
        open=True,
        blocking_count=blocking_count,
        has_in_flight=has_in_flight,
        has_unknown=has_unknown,
    )


def _persist_unknown_diagnostic(order: AliExpressOrder, *, session) -> None:
    """Persist the per-provider diagnostic code on the durable unknown attempt.

    The code comes exclusively from a ContextVar populated by the real provider
    through an allow-list. If no diagnostic exists, this function is a no-op.
    It never changes the parent error/state and never creates a new attempt.
    """
    code = consume_unknown_outcome()
    if not code:
        return

    attempt = (
        session.query(AliExpressSubmissionAttempt)
        .filter_by(
            aliexpress_order_id=order.id,
            attempt_key=order.attempt_key,
            state="unknown",
        )
        .first()
    )
    if attempt is None:
        return

    attempt.last_error = code
    attempt.updated_at = utcnow()
    session.commit()


def submit_controlled_canary_order(
    ali_order: AliExpressOrder,
    *,
    submitter: Optional[AliExpressOrderSubmitter],
    refresher=None,
    session=None,
    pre_submit_guard: Optional[Callable[..., None]] = None,
) -> FulfillmentResult:
    if submitter is None:
        raise AliExpressCanaryConfigurationError(
            "controlled_canary_requires_explicit_submitter"
        )

    _assert_canary_armed_for(ali_order.id)
    session = session or db.session

    with _global_canary_guard(session):
        order = (
            session.query(AliExpressOrder)
            .filter_by(id=ali_order.id)
            .populate_existing()
            .first()
        )
        if order is None:
            raise ValueError("aliexpress_order_not_found")

        status = get_canary_circuit_status(session=session)
        if status.open:
            raise AliExpressCanaryCircuitOpen("submission_circuit_open")

        if pre_submit_guard is not None:
            pre_submit_guard(order, session=session)

        clear_unknown_outcome()
        try:
            result = submit_aliexpress_order(
                order,
                submitter=submitter,
                refresher=refresher,
                session=session,
            )
            if result.state == "pending_reconciliation":
                _persist_unknown_diagnostic(order, session=session)
            else:
                consume_unknown_outcome()
            return result
        finally:
            clear_unknown_outcome()


__all__ = [
    "AliExpressCanaryCircuitOpen",
    "AliExpressCanaryConfigurationError",
    "CanaryCircuitStatus",
    "get_canary_circuit_status",
    "submit_controlled_canary_order",
]
