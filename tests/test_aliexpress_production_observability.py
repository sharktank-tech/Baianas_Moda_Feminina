"""FASE 8.1.0 — observabilidade agregada e sem dados sensíveis."""

import uuid
from types import SimpleNamespace

from loja import db
from loja.integrations.aliexpress import production_observability as module
from loja.modules.models import AliExpressOrder, AliExpressSubmissionAttempt


def _order(state):
    obj = AliExpressOrder(
        id=uuid.uuid4(),
        state=state,
        idempotency_key=uuid.uuid4().hex,
        attempt_count=0,
    )
    db.session.add(obj)
    db.session.flush()
    return obj


def test_snapshot_reports_gates_breaker_and_relative_counts(app, monkeypatch):
    with app.app_context():
        baseline_ready = AliExpressOrder.query.filter_by(state="ready").count()
        baseline_blocked = AliExpressOrder.query.filter_by(state="blocked").count()
        baseline_pending = AliExpressOrder.query.filter_by(state="pending_reconciliation").count()
        baseline_submitting = AliExpressOrder.query.filter_by(state="submitting").count()
        baseline_submitted = AliExpressOrder.query.filter_by(state="submitted").count()
        baseline_unresolved = AliExpressSubmissionAttempt.query.filter(
            AliExpressSubmissionAttempt.state.in_(("in_flight", "unknown")),
            AliExpressSubmissionAttempt.completed_at.is_(None),
        ).count()

        ready = _order("ready")
        _order("blocked")
        _order("pending_reconciliation")
        _order("submitting")
        _order("submitted")
        db.session.add(
            AliExpressSubmissionAttempt(
                id=uuid.uuid4(),
                aliexpress_order_id=ready.id,
                attempt_key=uuid.uuid4().hex,
                state="in_flight",
                completed_at=None,
            )
        )
        db.session.commit()

        monkeypatch.setattr(module, "postpayment_autodispatch_enabled", lambda: True)
        monkeypatch.setattr(
            module,
            "production_fulfillment_gate_state",
            lambda: SimpleNamespace(
                production_enabled=True,
                placeorder_enabled=True,
                can_select_real_provider=True,
            ),
        )
        monkeypatch.setattr(
            module,
            "evaluate_production_circuit_breaker",
            lambda **kwargs: SimpleNamespace(
                open=True,
                code="production_circuit_open_unresolved_attempt",
            ),
        )

        snapshot = module.get_production_observability_snapshot(session=db.session)

        assert snapshot.postpayment_autodispatch_enabled is True
        assert snapshot.production_fulfillment_enabled is True
        assert snapshot.placeorder_enabled is True
        assert snapshot.all_submission_gates_enabled is True
        assert snapshot.circuit_open is True
        assert snapshot.circuit_code == "production_circuit_open_unresolved_attempt"
        assert snapshot.ready_count == baseline_ready + 1
        assert snapshot.blocked_count == baseline_blocked + 1
        assert snapshot.pending_reconciliation_count == baseline_pending + 1
        assert snapshot.submitting_count == baseline_submitting + 1
        assert snapshot.submitted_count == baseline_submitted + 1
        assert snapshot.unresolved_attempt_count == baseline_unresolved + 1


def test_snapshot_fails_closed_when_any_gate_is_off(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(module, "postpayment_autodispatch_enabled", lambda: False)
        monkeypatch.setattr(
            module,
            "production_fulfillment_gate_state",
            lambda: SimpleNamespace(
                production_enabled=True,
                placeorder_enabled=True,
                can_select_real_provider=True,
            ),
        )
        monkeypatch.setattr(
            module,
            "evaluate_production_circuit_breaker",
            lambda **kwargs: SimpleNamespace(open=False, code=None),
        )

        snapshot = module.get_production_observability_snapshot(session=db.session)
        assert snapshot.all_submission_gates_enabled is False
        assert snapshot.circuit_open is False


def test_snapshot_schema_does_not_expose_sensitive_fields(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(module, "postpayment_autodispatch_enabled", lambda: False)
        monkeypatch.setattr(
            module,
            "production_fulfillment_gate_state",
            lambda: SimpleNamespace(
                production_enabled=False,
                placeorder_enabled=False,
                can_select_real_provider=False,
            ),
        )
        monkeypatch.setattr(
            module,
            "evaluate_production_circuit_breaker",
            lambda **kwargs: SimpleNamespace(open=False, code=None),
        )

        fields = set(module.get_production_observability_snapshot().__dict__)
        forbidden = {
            "cpf",
            "access_token",
            "refresh_token",
            "attempt_key",
            "ali_order_id",
            "remote_order_id",
            "raw_payload",
            "env_value",
        }
        assert fields.isdisjoint(forbidden)
