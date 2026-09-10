from __future__ import annotations

import os
from dataclasses import dataclass
from unittest import mock

import pytest

from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.placeorder_provider import TopAliExpressOrderSubmitter
from loja.integrations.aliexpress.submission import (
    AliExpressPlaceOrderItem,
    AliExpressSubmissionRequest,
    AliExpressSubmissionUnknown,
)
from loja.integrations.aliexpress.top_placeorder import TopPlaceOrderResponse
from loja.integrations.aliexpress.transport import TransportError, TransportTimeout


@dataclass
class _Capture:
    calls: list[tuple[str, dict]]

    def __call__(self, event, **fields):
        self.calls.append((event, fields))


def _request():
    return AliExpressSubmissionRequest(
        aliexpress_order_id="local-order",
        order_id=None,
        supplier_id="supplier",
        attempt_key="attempt",
        shipping_address={
            "address": "Rua Privada 1",
            "city": "Cidade",
            "contact_person": "Pessoa Privada",
            "country": "BR",
            "mobile_no": "11999999999",
            "province": "SP",
            "zip": "00000-000",
        },
        items=(
            AliExpressPlaceOrderItem(
                product_id="123456789",
                sku_attr="14:771;200000124:100013888",
                product_count=1,
            ),
        ),
    )


def _submitter():
    return TopAliExpressOrderSubmitter(
        app_key="app-key-secret-value",
        app_secret="app-secret-value",
        resolve_access_token=lambda supplier_id: "access-token-secret-value",
    )


def _run_with_exception(monkeypatch, exc, expected_code):
    capture = _Capture([])
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.log_placeorder_event",
        capture,
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.call_top_placeorder",
        lambda **kwargs: (_ for _ in ()).throw(exc),
    )

    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        with pytest.raises(AliExpressSubmissionUnknown) as raised:
            _submitter().submit(_request())

    assert raised.value.args[0] == expected_code
    unknown = [fields for event, fields in capture.calls if event == "provider_unknown"]
    assert len(unknown) == 1
    assert unknown[0]["outcome_code"] == expected_code
    assert isinstance(unknown[0]["duration_ms"], (int, float))

    rendered = repr(capture.calls)
    for forbidden in (
        "Rua Privada",
        "Pessoa Privada",
        "11999999999",
        "app-key-secret-value",
        "app-secret-value",
        "access-token-secret-value",
    ):
        assert forbidden not in rendered


def test_timeout_gets_specific_sanitized_outcome(monkeypatch):
    _run_with_exception(
        monkeypatch,
        TransportTimeout("provider details must not leak"),
        "placeorder_timeout",
    )


def test_transport_error_gets_specific_sanitized_outcome(monkeypatch):
    _run_with_exception(
        monkeypatch,
        TransportError("HTTP/provider details must not leak"),
        "placeorder_transport_error",
    )


def test_protocol_error_gets_specific_sanitized_outcome(monkeypatch):
    _run_with_exception(
        monkeypatch,
        AliExpressAPIError("raw provider response must not leak"),
        "placeorder_protocol_error",
    )


def test_remote_error_gets_specific_sanitized_outcome(monkeypatch):
    capture = _Capture([])
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.log_placeorder_event",
        capture,
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.call_top_placeorder",
        lambda **kwargs: TopPlaceOrderResponse(
            is_success=False,
            order_ids=(),
            error_code="UNLISTED_REMOTE_CODE",
            error_sub_code=None,
            error_message="sensitive remote message",
        ),
    )

    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        with pytest.raises(AliExpressSubmissionUnknown) as raised:
            _submitter().submit(_request())

    assert raised.value.args[0] == "placeorder_remote_error"
    unknown = [fields for event, fields in capture.calls if event == "provider_unknown"]
    assert len(unknown) == 1
    assert unknown[0]["outcome_code"] == "placeorder_remote_error"
    assert unknown[0]["is_success"] is False
    assert unknown[0]["remote_order_count"] == 0
    assert "sensitive remote message" not in repr(capture.calls)


def test_success_without_identity_is_diagnosed_without_ids(monkeypatch):
    capture = _Capture([])
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.log_placeorder_event",
        capture,
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.call_top_placeorder",
        lambda **kwargs: TopPlaceOrderResponse(
            is_success=True,
            order_ids=(),
            error_code=None,
            error_sub_code=None,
        ),
    )

    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        with pytest.raises(AliExpressSubmissionUnknown) as raised:
            _submitter().submit(_request())

    assert raised.value.args[0] == "placeorder_success_without_order_id"
    unknown = [fields for event, fields in capture.calls if event == "provider_unknown"]
    assert unknown[0]["outcome_code"] == "placeorder_success_without_order_id"
    assert unknown[0]["remote_order_count"] == 0


def test_confirmed_success_logs_only_cardinality(monkeypatch):
    capture = _Capture([])
    remote_id = "998877665544332211"
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.log_placeorder_event",
        capture,
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.placeorder_provider.call_top_placeorder",
        lambda **kwargs: TopPlaceOrderResponse(
            is_success=True,
            order_ids=(remote_id,),
            error_code=None,
            error_sub_code=None,
        ),
    )

    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        result = _submitter().submit(_request())

    assert result.external_order_ids == (remote_id,)
    confirmed = [fields for event, fields in capture.calls if event == "provider_confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0]["remote_order_count"] == 1
    assert remote_id not in repr(capture.calls)
