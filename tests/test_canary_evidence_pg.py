"""Private evidence ledger in an isolated disposable PostgreSQL schema."""

import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

from test_aliexpress_payment_guard_pg import env, _materialize_order, IS_POSTGRES, DATABASE_URL  # noqa: F401

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requer PostgreSQL descartável")


@pytest.fixture
def prepared(env, monkeypatch):
    schema, app = env
    from loja import db
    from loja.modules.models import User

    order_id = _materialize_order(app, committed_status="paid", payment_status="approved")
    ids = {}
    with app.app_context():
        for role in ("technical", "finance", "support", "reviewer", "outsider"):
            user = User(id=uuid.uuid4(), name=role, email=f"{uuid.uuid4()}@pg.test", is_admin=True)
            user.set_password("synthetic")
            db.session.add(user)
            ids[role] = user.id
        db.session.commit()
    for role in ("technical", "finance", "support", "reviewer"):
        monkeypatch.setenv("CANARY_EVIDENCE_%s_USER_IDS" % role.upper(), str(ids[role]))
    return app, schema, order_id, ids


def _actor(role, ids):
    from loja import db
    from loja.modules.models import User
    return db.session.get(User, ids[role])


def _case(app, order_id, ids, *, purpose="organic_candidate"):
    with app.app_context():
        from loja.services.canary_evidence import open_case
        return open_case(order_id, _actor("technical", ids), purpose).id


def _evidence(app, case_id, ids):
    from loja.services.canary_evidence import record
    with app.app_context():
        record(case_id, _actor("technical", ids), "technical", "passed")
        record(case_id, _actor("finance", ids), "payment", "confirmed", uuid.uuid4())
        record(case_id, _actor("support", ids), "customer", "confirmed", uuid.uuid4())


def test_roles_and_independent_evidence(prepared):
    app, _, order_id, ids = prepared
    from loja.services.canary_evidence import EvidenceRejected, open_case, record
    with app.app_context():
        with pytest.raises(EvidenceRejected, match="access_denied"):
            open_case(order_id, _actor("outsider", ids))
    case_id = _case(app, order_id, ids)
    with app.app_context():
        with pytest.raises(EvidenceRejected, match="access_denied"):
            record(case_id, _actor("technical", ids), "payment", "confirmed", uuid.uuid4())
        with pytest.raises(EvidenceRejected, match="invalid_identifier"):
            record(case_id, _actor("finance", ids), "payment", "confirmed", "client@example.com")
        with pytest.raises(EvidenceRejected, match="evidence_pending"):
            record(case_id, _actor("reviewer", ids), "decision", "confirmed")
    _evidence(app, case_id, ids)
    with app.app_context():
        assert record(case_id, _actor("reviewer", ids), "decision", "confirmed") == "confirmed"
        with pytest.raises(EvidenceRejected, match="case_closed"):
            record(case_id, _actor("reviewer", ids), "decision", "confirmed")


def test_simulated_purchase_never_confirms_organic_origin(prepared):
    app, _, order_id, ids = prepared
    case_id = _case(app, order_id, ids, purpose="simulation")
    _evidence(app, case_id, ids)
    from loja.services.canary_evidence import EvidenceRejected, record
    with app.app_context():
        with pytest.raises(EvidenceRejected, match="simulation_not_organic"):
            record(case_id, _actor("reviewer", ids), "decision", "confirmed")


def test_latest_reversal_blocks_and_no_supplier_side_effect(prepared):
    app, _, order_id, ids = prepared
    case_id = _case(app, order_id, ids)
    _evidence(app, case_id, ids)
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Payment
    from loja.modules.models import AliExpressSubmissionAttempt, AliExpressRemoteOrder
    from loja.services.canary_evidence import EvidenceRejected, record
    with app.app_context():
        payment = Payment.query.filter_by(order_id=order_id).first()
        payment.status = "refunded"
        db.session.commit()
        with pytest.raises(EvidenceRejected, match="payment_not_current"):
            record(case_id, _actor("reviewer", ids), "decision", "confirmed")
        assert AliExpressSubmissionAttempt.query.count() == 0
        assert AliExpressRemoteOrder.query.count() == 0


def test_events_are_immutable_in_database(prepared):
    app, _, order_id, ids = prepared
    case_id = _case(app, order_id, ids)
    from loja import db
    from loja.modules.canary_evidence_models import CanaryEvidenceEvent
    from loja.services.canary_evidence import record
    with app.app_context():
        record(case_id, _actor("technical", ids), "technical", "passed")
        event_id = CanaryEvidenceEvent.query.filter_by(case_id=case_id).first().id
        with pytest.raises(DBAPIError):
            db.session.execute(text("DELETE FROM canary_evidence_events WHERE id=:id"), {"id": event_id})
        db.session.rollback()
        with pytest.raises(DBAPIError):
            db.session.execute(text("UPDATE canary_evidence_events SET result='failed' WHERE id=:id"), {"id": event_id})
        db.session.rollback()
        assert CanaryEvidenceEvent.query.filter_by(case_id=case_id).count() == 1


def test_http_requires_role_and_csrf_and_hides_source_references(prepared, monkeypatch):
    app, _, order_id, ids = prepared
    case_id = _case(app, order_id, ids)
    monkeypatch.setenv("CANARY_EVIDENCE_REGISTER_ENABLED", "1")
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(ids["outsider"])
        session["_fresh"] = True
    assert client.get(f"/admin/canary-evidence/cases/{case_id}").status_code == 403
    with client.session_transaction() as session:
        session["_user_id"] = str(ids["finance"])
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/admin/canary-evidence/cases/events", json={}).status_code == 400
    response = client.get(f"/admin/canary-evidence/cases/{case_id}")
    assert response.status_code == 200
    assert "order_id" not in response.get_json()
    assert "evidence_ref" not in response.get_json()


def test_competing_decisions_only_one_commits(prepared):
    app, _, order_id, ids = prepared
    case_id = _case(app, order_id, ids)
    _evidence(app, case_id, ids)
    barrier = threading.Barrier(2)
    results = []
    def decide():
        with app.app_context():
            from loja.services.canary_evidence import EvidenceRejected, record
            barrier.wait(timeout=15)
            try:
                results.append(record(case_id, _actor("reviewer", ids), "decision", "confirmed"))
            except EvidenceRejected as exc:
                results.append(str(exc))
    threads = [threading.Thread(target=decide) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert sorted(results) == ["case_closed", "confirmed"]



def test_refund_commit_wins_over_pending_confirmation(prepared):
    app, _, order_id, ids = prepared
    case_id = _case(app, order_id, ids)
    _evidence(app, case_id, ids)
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order, Payment
    from loja.services.canary_evidence import EvidenceRejected, record

    refund_locked = threading.Event()
    decision_started = threading.Event()
    release_refund = threading.Event()
    results = []
    errors = []
    pids = {}

    def refund():
        try:
            with app.app_context():
                order = Order.query.filter_by(id=order_id).with_for_update(of=Order).one()
                pids['refund'] = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
                order.status = "refunded"
                payment = Payment.query.filter_by(order_id=order_id).first()
                payment.status = "refunded"
                db.session.flush()
                refund_locked.set()
                assert release_refund.wait(15)
                db.session.commit()
        except Exception as exc:
            errors.append(exc)
            refund_locked.set()
            release_refund.set()

    def decide():
        try:
            with app.app_context():
                assert refund_locked.wait(15)
                pids['decision'] = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
                decision_started.set()
                try:
                    results.append(record(case_id, _actor("reviewer", ids), "decision", "confirmed"))
                except EvidenceRejected as exc:
                    results.append(str(exc))
        except Exception as exc:
            errors.append(exc)
            decision_started.set()
            release_refund.set()

    threads = [threading.Thread(target=refund), threading.Thread(target=decide)]
    for thread in threads:
        thread.start()
    try:
        assert decision_started.wait(15)
        assert not errors, errors
        assert 'refund' in pids, errors
        observer = create_engine(DATABASE_URL)
        try:
            with observer.connect() as conn:
                deadline = time.monotonic() + 10
                blocked = False
                while time.monotonic() < deadline:
                    blockers = conn.execute(text('SELECT pg_blocking_pids(:pid)'),
                                            {'pid': pids['decision']}).scalar_one()
                    if pids['refund'] in blockers:
                        blocked = True
                        break
                    if results or errors:
                        break
                    time.sleep(0.02)
                assert blocked, 'decision did not wait for refund order lock'
        finally:
            observer.dispose()
    finally:
        release_refund.set()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert errors == []
    assert results == ["payment_not_current"]


def test_reviewed_migration_is_idempotent_in_disposable_schema(env):
    schema, _ = env
    script = (Path(__file__).resolve().parents[1] / "migrations" /
              "0006_canary_evidence.sql").read_text(encoding="utf-8")
    # Replace the reviewed target in-memory for this disposable schema only.
    script = script.replace("baianas_moda_feminina", schema)
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='public' ")).scalar_one()
            conn.commit()
            for _ in range(2):
                conn.exec_driver_sql(script)
                conn.commit()
            after = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='public' ")).scalar_one()
            assert before == after
            trigger_count = conn.execute(text("""
                SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=:schema AND c.relname='canary_evidence_events'
                  AND t.tgname='canary_evidence_immutable'
            """), {"schema": schema}).scalar_one()
            assert trigger_count == 1
    finally:
        engine.dispose()
