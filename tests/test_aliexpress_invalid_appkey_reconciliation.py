"""FASE 8.0.11T — rejeição oficial por App Key e reconciliação offline.

Cobertura totalmente local: zero credencial e zero rede.
"""

import uuid

import pytest

from loja.integrations.aliexpress.placeorder_provider import _classify_placeorder
from loja.integrations.aliexpress.reconciliation import _offline_durable_result
from loja.integrations.aliexpress.reconciliation_diagnostics import (
    DURABLE_RECONCILE_NOT_FOUND,
    classify_durable_unknown_for_reconciliation,
)
from loja.integrations.aliexpress.submission import (
    AliExpressSubmissionFailed,
    AliExpressSubmissionUnknown,
)
from loja.integrations.aliexpress.top_placeorder import TopPlaceOrderResponse


EXACT_DURABLE = (
    "placeorder_remote_error|remote_error_code=29|"
    "remote_error_sub_code=isv.appkey-not-exists"
)


def _failure(*, code=None, sub_code=None):
    return TopPlaceOrderResponse(
        is_success=False,
        order_ids=(),
        error_code=code,
        error_sub_code=sub_code,
    )


def test_exact_official_invalid_appkey_pair_is_confirmed_failure():
    with pytest.raises(AliExpressSubmissionFailed) as exc:
        _classify_placeorder(
            _failure(code="29", sub_code="isv.appkey-not-exists")
        )
    assert exc.value.args[0] == "29"


def test_code_29_alone_remains_unknown():
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        _classify_placeorder(_failure(code="29"))
    assert exc.value.args[0] == "placeorder_remote_error"


def test_appkey_subcode_alone_remains_unknown():
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        _classify_placeorder(_failure(sub_code="isv.appkey-not-exists"))
    assert exc.value.args[0] == "placeorder_remote_error"


def test_code_29_with_different_subcode_remains_unknown():
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        _classify_placeorder(
            _failure(code="29", sub_code="isv.some-other-error")
        )
    assert exc.value.args[0] == "placeorder_remote_error"


def test_exact_durable_diagnostic_reconciles_not_found_offline():
    assert (
        classify_durable_unknown_for_reconciliation(EXACT_DURABLE)
        == DURABLE_RECONCILE_NOT_FOUND
    )


@pytest.mark.parametrize(
    "value",
    [
        "placeorder_remote_error|remote_error_code=29",
        "placeorder_remote_error|remote_error_sub_code=isv.appkey-not-exists",
        "placeorder_remote_error|remote_error_code=29|remote_error_sub_code=isv.other",
        "placeorder_timeout|remote_error_code=29|remote_error_sub_code=isv.appkey-not-exists",
        "free text 29 isv.appkey-not-exists",
        None,
    ],
)
def test_non_exact_durable_evidence_remains_inconclusive(value):
    assert classify_durable_unknown_for_reconciliation(value) is None


class _Attempt:
    def __init__(self, *, state="unknown", last_error=EXACT_DURABLE):
        self.state = state
        self.last_error = last_error


class _Query:
    def __init__(self, attempt):
        self.attempt = attempt

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self.attempt


class _Session:
    def __init__(self, attempt):
        self.attempt = attempt

    def query(self, model):
        return _Query(self.attempt)


class _Order:
    id = uuid.uuid4()
    attempt_key = "attempt-safe-test"


def test_offline_boundary_uses_only_current_unknown_attempt():
    result = _offline_durable_result(
        _Order(), session=_Session(_Attempt(state="unknown"))
    )
    assert result is not None
    assert result.outcome == "not_found"


def test_offline_boundary_does_not_reopen_resolved_attempt():
    result = _offline_durable_result(
        _Order(), session=_Session(_Attempt(state="rejected"))
    )
    assert result is None
