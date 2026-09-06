"""PostgreSQL proof for the Fase 8.0.11I durable one-shot canary.

Uses the disposable-schema fixture from the H concurrency proof and an injected
fake submitter.  No AliExpress HTTP is performed.
"""

import threading

import pytest

from loja.integrations.aliexpress.single_live_canary import (
    AliExpressSingleLiveCanaryConsumed,
    submit_single_live_canary_order,
)
from tests.test_aliexpress_controlled_canary_pg import (
    IS_POSTGRES,
    SlowCountingSubmitter,
    _materialize_ready,
    env,
)

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL (DATABASE_URL postgresql://)",
)


def _start_single(app, ali_order_id, submitter, results, errors, barrier):
    from loja import db
    from loja.modules.models import AliExpressOrder

    try:
        with app.app_context():
            db.session.connection().exec_driver_sql(
                "SET SESSION lock_timeout = 15000"
            )
            barrier.wait(timeout=30)
            ali_order = db.session.get(AliExpressOrder, ali_order_id)
            result = submit_single_live_canary_order(
                ali_order,
                submitter=submitter,
                session=db.session,
            )
            results.append(result.state)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_two_concurrent_single_live_calls_consume_exactly_one_slot(env):
    schema, app, monkeypatch = env
    ali_order_id = _materialize_ready(app)

    # Test-only arming.  The injected fake submitter guarantees zero network.
    monkeypatch.setenv("ALIEXPRESS_SINGLE_LIVE_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ENABLED", "1")
    monkeypatch.setenv("ALIEXPRESS_CONTROLLED_CANARY_ORDER_ID", str(ali_order_id))
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    submitter = SlowCountingSubmitter()
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_single,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_single,
        args=(app, ali_order_id, submitter, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert results == ["submitted"]
    assert len(errors) == 1
    assert isinstance(errors[0], AliExpressSingleLiveCanaryConsumed)
    assert str(errors[0]) == "single_live_canary_already_consumed"
    assert submitter.call_count == 1

    from loja import db
    from loja.modules.models import (
        AliExpressOrder,
        AliExpressRemoteOrder,
        AliExpressSubmissionAttempt,
    )

    with app.app_context():
        row = db.session.get(AliExpressOrder, ali_order_id)
        assert row.state == "submitted"
        assert AliExpressSubmissionAttempt.query.count() == 1
        assert AliExpressRemoteOrder.query.filter_by(
            aliexpress_order_id=ali_order_id
        ).count() == 1
