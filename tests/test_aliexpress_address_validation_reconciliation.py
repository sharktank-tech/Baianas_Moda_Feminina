"""FASE 8.0.11W — rejeição oficial de endereço e reconciliação offline.

Cobertura totalmente local: zero credencial, zero rede e zero payload sensível.
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


ADDRESS_CODE = "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"
EXACT_DURABLE = f"placeorder_remote_error|remote_error_code={ADDRESS_CODE}"


def _failure(*, code=None, sub_code=None):
    return TopPlaceOrderResponse(
        is_success=False,
        order_ids=(),
        error_code=code,
        error_sub_code=sub_code,
    )


def test_exact_address_validation_code_is_confirmed_failure():
    with pytest.raises(AliExpressSubmissionFailed) as exc:
        _classify_placeorder(_failure(code=ADDRESS_CODE))
    assert exc.value.args[0] == ADDRESS_CODE


def test_similar_address_code_remains_unknown():
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        _classify_placeorder(_failure(code=ADDRESS_CODE + "_OTHER"))
    assert exc.value.args[0] == "placeorder_remote_error"


def test_exact_durable_address_diagnostic_reconciles_not_found_offline():
    assert (
        classify_durable_unknown_for_reconciliation(EXACT_DURABLE)
        == DURABLE_RECONCILE_NOT_FOUND
    )


@pytest.mark.parametrize(
    "value",
    [
        f"placeorder_remote_error|remote_error_code={ADDRESS_CODE}_OTHER",
        f"placeorder_timeout|remote_error_code={ADDRESS_CODE}",
        f"placeorder_remote_error|remote_error_code={ADDRESS_CODE}|remote_error_sub_code=other",
        ADDRESS_CODE,
        None,
    ],
)
def test_non_exact_address_evidence_remains_inconclusive(value):
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
    attempt_key = "attempt-address-validation-test"


def test_offline_boundary_resolves_current_unknown_address_rejection():
    result = _offline_durable_result(
        _Order(), session=_Session(_Attempt(state="unknown"))
    )
    assert result is not None
    assert result.outcome == "not_found"


def test_offline_boundary_does_not_reopen_resolved_address_attempt():
    result = _offline_durable_result(
        _Order(), session=_Session(_Attempt(state="rejected"))
    )
    assert result is None
