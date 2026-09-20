"""Private canary evidence ledger. Never used as an AliExpress execution gate."""

import uuid

from sqlalchemy import DDL, event
from sqlalchemy.dialects.postgresql import UUID

from loja import db
from loja.utils import utcnow


class CanaryEvidenceCase(db.Model):
    __tablename__ = "canary_evidence_cases"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = db.Column(UUID(as_uuid=True), db.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True)
    purpose = db.Column(db.String(20), nullable=False, default="simulation", server_default="simulation")
    state = db.Column(db.String(16), nullable=False, default="pending", server_default="pending")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, server_default=db.func.now())
    decided_at = db.Column(db.DateTime)

    __table_args__ = (
        db.CheckConstraint("state IN ('pending','confirmed','rejected')", name="ck_canary_evidence_case_state"),
        db.CheckConstraint("purpose IN ('simulation','organic_candidate')", name="ck_canary_evidence_case_purpose"),
    )


class CanaryEvidenceEvent(db.Model):
    __tablename__ = "canary_evidence_events"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = db.Column(UUID(as_uuid=True), db.ForeignKey("canary_evidence_cases.id", ondelete="RESTRICT"), nullable=False, index=True)
    actor_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    result = db.Column(db.String(16), nullable=False)
    # UUID only: no screenshot, CPF, address, provider payload, conversation or URL.
    evidence_ref = db.Column(UUID(as_uuid=True))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, server_default=db.func.now())

    __table_args__ = (
        db.CheckConstraint("(kind = 'technical' AND result IN ('passed','failed')) OR "
                           "(kind = 'payment' AND result IN ('confirmed','divergent')) OR "
                           "(kind = 'customer' AND result IN ('confirmed','denied','no_response')) OR "
                           "(kind = 'decision' AND result IN ('confirmed','rejected'))",
                           name="ck_canary_evidence_event_kind_result"),
        db.CheckConstraint("(kind IN ('payment','customer') AND evidence_ref IS NOT NULL) OR "
                           "(kind IN ('technical','decision') AND evidence_ref IS NULL)",
                           name="ck_canary_evidence_event_reference"),
    )


# Installed only when a controlled bootstrap creates the new table. Existing
# deployments must apply the reviewed migration separately; cold starts do no DDL.
event.listen(CanaryEvidenceEvent.__table__, "after_create", DDL("""
CREATE FUNCTION canary_evidence_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'canary evidence events are append only';
END $$;
CREATE TRIGGER canary_evidence_immutable
BEFORE UPDATE OR DELETE ON canary_evidence_events
FOR EACH ROW EXECUTE FUNCTION canary_evidence_reject_mutation();
""").execute_if(dialect="postgresql"))
