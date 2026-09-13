"""FASE 8.1.0 — testes do orquestrador one-shot de produção."""

from types import SimpleNamespace

from loja.integrations.aliexpress.production_fulfillment import (
    ProductionFulfillmentGateState,
)
from loja.integrations.aliexpress.production_orchestrator import (
    ORCHESTRATOR_DISABLED,
    ORCHESTRATOR_DISPATCHED,
    orchestrate_production_submission,
)
from loja.integrations.aliexpress.production_preflight import (
    ProductionSubmissionEligibility,
)


def _eligibility(*, eligible=True, code=None):
    return ProductionSubmissionEligibility(
        eligible=eligible,
        code=code,
        attempt_count=0,
        attempt_rows=0,
        snapshot_present=True,
    )


def _enabled_gates():
    return ProductionFulfillmentGateState(
        production_enabled=True,
        placeorder_enabled=True,
    )


def _disabled_gates():
    return ProductionFulfillmentGateState(
        production_enabled=False,
        placeorder_enabled=False,
    )


def test_ineligible_preflight_never_selects_or_submits():
    order = SimpleNamespace(state="ready")
    calls = {"factory": 0, "submit": 0}

    def factory():
        calls["factory"] += 1
        return object()

    def submit(*args, **kwargs):
        calls["submit"] += 1
        raise AssertionError("submit não deveria ser chamado")

    result = orchestrate_production_submission(
        order,
        preflight_fn=lambda *args, **kwargs: _eligibility(
            eligible=False,
            code="production_preflight_attempt_unresolved",
        ),
        gate_state_fn=_enabled_gates,
        submitter_factory=factory,
        submit_fn=submit,
    )

    assert result.dispatched is False
    assert result.eligible is False
    assert result.code == "production_preflight_attempt_unresolved"
    assert result.state == "ready"
    assert calls == {"factory": 0, "submit": 0}


def test_disabled_gates_never_select_provider_or_submit():
    order = SimpleNamespace(state="ready")
    calls = {"factory": 0, "submit": 0}

    def factory():
        calls["factory"] += 1
        return object()

    def submit(*args, **kwargs):
        calls["submit"] += 1
        raise AssertionError("submit não deveria ser chamado")

    result = orchestrate_production_submission(
        order,
        preflight_fn=lambda *args, **kwargs: _eligibility(),
        gate_state_fn=_disabled_gates,
        submitter_factory=factory,
        submit_fn=submit,
    )

    assert result.dispatched is False
    assert result.eligible is True
    assert result.code == ORCHESTRATOR_DISABLED
    assert result.state == "ready"
    assert calls == {"factory": 0, "submit": 0}


def test_eligible_enabled_path_submits_exactly_once():
    order = SimpleNamespace(state="ready")
    submitter = object()
    calls = {"factory": 0, "submit": 0}

    def factory():
        calls["factory"] += 1
        return submitter

    def submit(target, *, submitter, refresher, session):
        calls["submit"] += 1
        assert target is order
        assert submitter is not None
        return SimpleNamespace(state="submitted")

    result = orchestrate_production_submission(
        order,
        preflight_fn=lambda *args, **kwargs: _eligibility(),
        gate_state_fn=_enabled_gates,
        submitter_factory=factory,
        submit_fn=submit,
    )

    assert result.dispatched is True
    assert result.eligible is True
    assert result.code == ORCHESTRATOR_DISPATCHED
    assert result.state == "submitted"
    assert calls == {"factory": 1, "submit": 1}


def test_pending_reconciliation_is_returned_without_retry():
    order = SimpleNamespace(state="ready")
    calls = {"submit": 0}

    def submit(*args, **kwargs):
        calls["submit"] += 1
        return SimpleNamespace(state="pending_reconciliation")

    result = orchestrate_production_submission(
        order,
        preflight_fn=lambda *args, **kwargs: _eligibility(),
        gate_state_fn=_enabled_gates,
        submitter_factory=lambda: object(),
        submit_fn=submit,
    )

    assert result.dispatched is True
    assert result.code == ORCHESTRATOR_DISPATCHED
    assert result.state == "pending_reconciliation"
    assert calls["submit"] == 1


def test_session_and_refresher_are_forwarded_once():
    order = SimpleNamespace(state="ready")
    session = object()
    refresher = object()
    seen = {}

    def preflight(target, *, session, refresher):
        seen["preflight"] = (target, session, refresher)
        return _eligibility()

    def submit(target, *, submitter, refresher, session):
        seen["submit"] = (target, submitter, session, refresher)
        return SimpleNamespace(state="submitted")

    submitter = object()
    result = orchestrate_production_submission(
        order,
        session=session,
        refresher=refresher,
        preflight_fn=preflight,
        gate_state_fn=_enabled_gates,
        submitter_factory=lambda: submitter,
        submit_fn=submit,
    )

    assert result.state == "submitted"
    assert seen["preflight"] == (order, session, refresher)
    assert seen["submit"] == (order, submitter, session, refresher)
