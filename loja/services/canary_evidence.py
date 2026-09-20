"""Restricted, append-only evidence service. Does not authorize supplier spending."""

import os
import uuid

from sqlalchemy.exc import IntegrityError

from loja import db
from loja.modules.canary_evidence_models import CanaryEvidenceCase, CanaryEvidenceEvent
from loja.modules.getway_mercado_pago.payment_models import Order, Payment
from loja.utils import utcnow


ROLE_FOR_KIND = {
    "technical": "technical", "payment": "finance",
    "customer": "support", "decision": "reviewer",
}


class EvidenceRejected(ValueError):
    """A sanitized failure; never includes a database record or source payload."""


def permitted(actor, role):
    if not actor or not actor.is_authenticated or not actor.is_admin:
        return False
    # Server-side assignment only; an unset role grants no access.
    configured = os.environ.get("CANARY_EVIDENCE_%s_USER_IDS" % role.upper(), "")
    ids = {s.strip() for s in configured.split(",") if s.strip()}
    return str(actor.id) in ids


def _latest(case_id, kind):
    return (CanaryEvidenceEvent.query.filter_by(case_id=case_id, kind=kind)
            .order_by(CanaryEvidenceEvent.created_at.desc(), CanaryEvidenceEvent.id.desc())
            .first())


def open_case(order_id, actor, purpose="simulation"):
    if not permitted(actor, "technical"):
        raise EvidenceRejected("access_denied")
    if purpose not in ("simulation", "organic_candidate"):
        raise EvidenceRejected("invalid_purpose")
    try:
        order_id = uuid.UUID(str(order_id))
    except (ValueError, TypeError):
        raise EvidenceRejected("invalid_identifier") from None
    if db.session.get(Order, order_id) is None:
        raise EvidenceRejected("order_not_found")
    case = CanaryEvidenceCase(order_id=order_id, purpose=purpose)
    db.session.add(case)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise EvidenceRejected("case_exists") from None
    return case


def record(case_id, actor, kind, result, evidence_ref=None):
    if not isinstance(kind, str) or kind not in ROLE_FOR_KIND:
        raise EvidenceRejected("invalid_event")
    if not permitted(actor, ROLE_FOR_KIND[kind]):
        raise EvidenceRejected("access_denied")
    try:
        case_id = uuid.UUID(str(case_id))
        ref = uuid.UUID(str(evidence_ref)) if evidence_ref is not None else None
    except (TypeError, ValueError):
        raise EvidenceRejected("invalid_identifier") from None
    allowed = {
        "technical": {"passed", "failed"},
        "payment": {"confirmed", "divergent"},
        "customer": {"confirmed", "denied", "no_response"},
        "decision": {"confirmed", "rejected"},
    }
    if (not isinstance(result, str) or result not in allowed[kind]
            or ((kind in {"payment", "customer"}) != (ref is not None))):
        raise EvidenceRejected("invalid_event")

    # Serialize every writer for a case; simultaneous decisions cannot both win.
    case = (db.session.query(CanaryEvidenceCase)
            .filter_by(id=case_id).with_for_update().one_or_none())
    if case is None:
        raise EvidenceRejected("case_not_found")
    if case.state != "pending":
        raise EvidenceRejected("case_closed")
    if kind == "decision" and result == "confirmed":
        if case.purpose != "organic_candidate":
            raise EvidenceRejected("simulation_not_organic")
        for source in ("technical", "payment", "customer"):
            latest = _latest(case.id, source)
            expected = "passed" if source == "technical" else "confirmed"
            if latest is None or latest.result != expected:
                raise EvidenceRejected("evidence_pending")
        # Recheck the current canonical payment. Evidence from the panel does not
        # replace the order/payment guard and cannot turn a refunded order paid.
        # _sync_payment also locks Order before changing payment/status. A
        # refund committed while this decision waits is therefore observed.
        order = (Order.query.filter_by(id=case.order_id)
                 .with_for_update(of=Order).first())
        payment = (Payment.query.filter_by(order_id=case.order_id)
                   .order_by(Payment.created_at.desc(), Payment.id.desc()).first())
        if (order is None or order.status != "paid" or payment is None
                or payment.status != "approved" or not payment.provider_payment_id
                or payment.currency != "BRL" or payment.amount != order.total):
            raise EvidenceRejected("payment_not_current")
    if kind == "decision":
        case.state = "confirmed" if result == "confirmed" else "rejected"
        case.decided_at = utcnow()
    db.session.add(CanaryEvidenceEvent(case_id=case.id, actor_id=actor.id,
                                       kind=kind, result=result, evidence_ref=ref))
    db.session.commit()
    return case.state
