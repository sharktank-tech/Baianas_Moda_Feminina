"""Fila read-only de exceções pós-venda (FASE 8.3.0-I/J)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional, Tuple

from loja.utils import utcnow


STALE_AFTER_ENV = "FULFILLMENT_STALE_AFTER_HOURS"
DEFAULT_STALE_AFTER_HOURS = 48
_MAX_STALE_AFTER_HOURS = 24 * 30


@dataclass(frozen=True)
class PostSaleExceptionRow:
    fulfillment_id: object
    order_number: Optional[str]
    supplier_name: Optional[str]
    state: str
    reasons: Tuple[str, ...]
    updated_at: object
    age_hours: Optional[int]


@dataclass(frozen=True)
class PostSaleExceptionSnapshot:
    rows: Tuple[PostSaleExceptionRow, ...]
    stale_after_hours: int

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def stalled(self) -> int:
        return sum(
            1 for row in self.rows
            if any(reason.endswith("_stalled") for reason in row.reasons)
        )


def load_stale_after_hours() -> int:
    raw = os.environ.get(STALE_AFTER_ENV)
    if raw is None or raw == "":
        return DEFAULT_STALE_AFTER_HOURS
    if raw != raw.strip() or not raw.isdigit():
        raise ValueError("fulfillment_stale_after_invalid")
    value = int(raw)
    if not 1 <= value <= _MAX_STALE_AFTER_HOURS:
        raise ValueError("fulfillment_stale_after_invalid")
    return value


def _age_hours(updated_at, now) -> Optional[int]:
    if updated_at is None:
        return None
    delta = now - updated_at
    return max(0, int(delta.total_seconds() // 3600))


def _reasons(fulfillment, *, cutoff) -> Tuple[str, ...]:
    reasons = []
    state = str(getattr(fulfillment, "state", "") or "")
    updated_at = getattr(fulfillment, "updated_at", None)
    stale = updated_at is None or updated_at <= cutoff

    if state == "manual_review":
        reasons.append("manual_review")
    if state == "pending_reconciliation":
        reasons.append("pending_reconciliation")
    if state == "failed":
        reasons.append("fulfillment_failed")
    if state == "cancel_requested" and stale:
        reasons.append("cancellation_stalled")
    if state in {"submitted", "confirmed", "shipped"} and stale:
        reasons.append("fulfillment_stalled")

    order = getattr(fulfillment, "order", None)
    refund_state = getattr(order, "supplier_refund_state", None) if order else None
    if refund_state in {"failed", "manual_review"}:
        reasons.append("refund_failure")
    if refund_state in {"requested", "pending_webhook"}:
        refund_updated = (
            getattr(order, "supplier_refund_requested_at", None)
            or getattr(order, "updated_at", None)
        )
        if refund_updated is None or refund_updated <= cutoff:
            reasons.append("refund_stalled")

    remotes = tuple(getattr(fulfillment, "remote_orders", None) or ())
    if state == "shipped" and stale and (
        not remotes
        or any(not getattr(remote, "tracking_code", None) for remote in remotes)
    ):
        reasons.append("tracking_missing")

    return tuple(dict.fromkeys(reasons))


def build_post_sale_exception_snapshot(
    fulfillments: Iterable[object],
    *,
    stale_after_hours: Optional[int] = None,
    now=None,
) -> PostSaleExceptionSnapshot:
    hours = (
        load_stale_after_hours()
        if stale_after_hours is None
        else stale_after_hours
    )
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= _MAX_STALE_AFTER_HOURS:
        raise ValueError("fulfillment_stale_after_invalid")
    now = now or utcnow()
    cutoff = now - timedelta(hours=hours)

    rows = []
    for fulfillment in fulfillments:
        reasons = _reasons(fulfillment, cutoff=cutoff)
        if not reasons:
            continue
        order = getattr(fulfillment, "order", None)
        supplier = getattr(fulfillment, "supplier", None)
        updated_at = getattr(fulfillment, "updated_at", None)
        rows.append(
            PostSaleExceptionRow(
                fulfillment_id=getattr(fulfillment, "id", None),
                order_number=getattr(order, "order_number", None),
                supplier_name=getattr(supplier, "name", None),
                state=str(getattr(fulfillment, "state", "") or ""),
                reasons=reasons,
                updated_at=updated_at,
                age_hours=_age_hours(updated_at, now),
            )
        )

    rows.sort(
        key=lambda row: (
            row.updated_at is not None,
            row.updated_at,
            str(row.fulfillment_id),
        )
    )
    return PostSaleExceptionSnapshot(tuple(rows), hours)
