"""Controlled live-canary guard for AliExpress placeorder (Fase 8.0.11H).

This module does NOT enable AliExpress HTTP and does not select the real
provider. It is an additional fail-closed boundary that must wrap every future
controlled live submission.

Safety contract:
* the controlled canary is OFF unless ``ALIEXPRESS_CONTROLLED_CANARY_ENABLED``
  is exactly ``1``;
* the canary is scoped to exactly one AliExpressOrder UUID through
  ``ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID``;
* only one controlled canary submission may enter the submission pipeline at
  a time in PostgreSQL (session-level advisory lock held on a dedicated DB
  connection, therefore preserved across the internal ORM commits);
* any durable ``in_flight`` attempt blocks every later controlled submission;
* any ``unknown`` attempt blocks every later controlled submission, even when
  ``completed_at`` is populated;
* the blocker is never released by age/timeouts;
* the caller must inject the submitter explicitly -- this module never chooses
  or enables the real provider;
* optional ``pre_submit_guard`` executes inside the same global advisory-lock
  critical section immediately before delegation to the core submission;
* no attempt key, remote order id, token, credential, payload or PII is exposed
  by the circuit status/error.

Why a PostgreSQL session advisory lock?
``submit_aliexpress_order`` intentionally commits the durable attempt before
provider HTTP. A transaction-scoped lock acquired by a wrapper would be
released by those commits. The dedicated session lock remains held across
those commits; if the process dies, PostgreSQL releases the lock while the
already committed ``in_flight`` attempt remains as the durable global blocker.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import text

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt

from .fulfillment import FulfillmentResult
from .submission import AliExpressOrderSubmitter, submit_aliexpress_order


# Fixed application lock id. It is not a secret and carries no business data.
_CANARY_ADVISORY_LOCK_KEY = 80110110011
_BLOCKING_ATTEMPT_STATES = ("in_flight", "unknown")
_CANARY_ENABLED_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ENABLED"
_CANARY_ORDER_ID_ENV = "ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID"


class AliExpressCanaryCircuitOpen(Exception):
    """A prior ambiguous/in-flight attempt blocks a new live canary.

    The message is intentionally fixed and contains no order/attempt/provider
    identity.
    """


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
    """Fail-closed gate: only exact string ``1`` enables the canary wrapper."""

    return os.environ.get(_CANARY_ENABLED_ENV) == "1"


def _canary_target_matches(order_id) -> bool:
    """Require an exact, non-empty target order id match.

    The configured UUID is treated only as an allow-list selector. It is never
    emitted in exceptions or status objects.
    """

    configured = os.environ.get(_CANARY_ORDER_ID_ENV)
    if not configured:
        return False
    return configured.strip() == str(order_id)


def _assert_canary_armed_for(order_id) -> None:
    """Require both the explicit canary gate and exact target-order allow-list."""

    if not _canary_enabled():
        raise AliExpressCanaryConfigurationError("controlled_canary_disabled")
    if not _canary_target_matches(order_id):
        raise AliExpressCanaryConfigurationError(
            "controlled_canary_target_mismatch"
        )


@contextmanager
def _global_canary_guard(session):
    """Serialize controlled submissions globally on PostgreSQL.

    SQLite/non-PostgreSQL test environments use a no-op guard; concurrency for
    the live canary is a PostgreSQL production invariant.
    """

    bind = session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        yield
        return

    # Dedicated connection is deliberate: the ORM submission pipeline commits
    # multiple transactions. A PostgreSQL session-level advisory lock on this
    # connection survives those ORM commits.
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
    """Return the durable global circuit status without exposing identities.

    ``unknown`` is always blocking. ``in_flight`` is always blocking. There is
    intentionally no age-based expiry because an old ``in_flight`` row can be
    the only evidence that provider execution may have happened.
    """

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


def submit_controlled_canary_order(
    ali_order: AliExpressOrder,
    *,
    submitter: Optional[AliExpressOrderSubmitter],
    refresher=None,
    session=None,
    pre_submit_guard: Optional[Callable[..., None]] = None,
) -> FulfillmentResult:
    """Submit exactly one allow-listed order through the canary boundary.

    Required independent conditions:
      1. explicit submitter injected by caller;
      2. ``ALIEXPRESS_CONTROLLED_CANARY_ENABLED == '1'``;
      3. ``ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID`` matches this order exactly;
      4. no global in_flight/unknown blocker exists.

    ``pre_submit_guard`` is an optional stricter policy hook.  When supplied it
    executes AFTER the global circuit check and WHILE the PostgreSQL advisory
    lock is still held.  The hook may only validate/fail closed; it must not
    perform provider I/O.

    This function still does not enable ``ALIEXPRESS_PLACEORDER_ENABLED`` and
    never chooses the real provider. Those are separate gates outside this
    module.
    """

    if submitter is None:
        raise AliExpressCanaryConfigurationError(
            "controlled_canary_requires_explicit_submitter"
        )

    _assert_canary_armed_for(ali_order.id)
    session = session or db.session

    with _global_canary_guard(session):
        # Re-read after obtaining the global guard. No IDs leave this boundary;
        # the lookup only proves that the target still exists.
        order = (
            session.query(AliExpressOrder)
            .filter_by(id=ali_order.id)
            .populate_existing()
            .first()
        )
        if order is None:
            raise ValueError("aliexpress_order_not_found")

        # Any blocker, including one belonging to this order, is fail-closed.
        # This is intentionally stricter than the historical per-order stale
        # barrier: controlled live canary never retries an ambiguous attempt.
        status = get_canary_circuit_status(session=session)
        if status.open:
            raise AliExpressCanaryCircuitOpen("submission_circuit_open")

        if pre_submit_guard is not None:
            pre_submit_guard(order, session=session)

        return submit_aliexpress_order(
            order,
            submitter=submitter,
            refresher=refresher,
            session=session,
        )


__all__ = [
    "AliExpressCanaryCircuitOpen",
    "AliExpressCanaryConfigurationError",
    "CanaryCircuitStatus",
    "get_canary_circuit_status",
    "submit_controlled_canary_order",
]
