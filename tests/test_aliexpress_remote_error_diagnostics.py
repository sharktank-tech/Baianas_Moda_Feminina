from __future__ import annotations

import logging

from loja.integrations.aliexpress.placeorder_diagnostics import (
    build_durable_unknown_error,
    clear_remote_error_metadata,
    consume_remote_error_metadata,
    log_placeorder_event,
    record_remote_error_metadata,
    response_error_metadata,
    sanitize_remote_code,
    validate_durable_unknown_error,
)
from loja.integrations.aliexpress.top_placeorder import TopPlaceOrderResponse


def test_sanitize_remote_code_accepts_machine_codes_only():
    assert sanitize_remote_code("400") == "400"
    assert sanitize_remote_code("INVALID_PARAMETER") == "INVALID_PARAMETER"
    assert sanitize_remote_code("ISV.PARAMETER-ERROR") == "ISV.PARAMETER-ERROR"
    assert sanitize_remote_code("isv.invalid-permission") == "isv.invalid-permission"


def test_sanitize_remote_code_rejects_free_form_or_suspicious_values():
    assert sanitize_remote_code(None) is None
    assert sanitize_remote_code(True) is None
    assert sanitize_remote_code("") is None
    assert sanitize_remote_code("invalid parameter") is None
    assert sanitize_remote_code("token=secret") is None
    assert sanitize_remote_code("foo:bar") is None
    assert sanitize_remote_code("/etc/passwd") is None
    assert sanitize_remote_code("A" * 65) is None


def test_response_error_metadata_never_exports_remote_message_or_order_ids():
    response = TopPlaceOrderResponse(
        is_success=False,
        order_ids=("987654321",),
        error_code="ISV.PARAMETER-ERROR",
        error_sub_code="isv.invalid-permission",
        error_message="token=super-secret customer@example.com",
    )

    metadata = response_error_metadata(response)

    assert metadata == {
        "remote_error_code": "ISV.PARAMETER-ERROR",
        "remote_error_sub_code": "isv.invalid-permission",
    }
    rendered = repr(metadata)
    assert "super-secret" not in rendered
    assert "customer@example.com" not in rendered
    assert "987654321" not in rendered


def test_response_error_metadata_drops_unsafe_remote_codes():
    response = TopPlaceOrderResponse(
        is_success=False,
        error_code="token=secret",
        error_sub_code="invalid parameter",
        error_message="sensitive free-form text",
    )

    assert response_error_metadata(response) == {}


def test_remote_error_metadata_context_is_sanitized_and_consumed_once():
    clear_remote_error_metadata()
    response = TopPlaceOrderResponse(
        is_success=False,
        error_code="ISV.PARAMETER-ERROR",
        error_sub_code="isv.invalid-permission",
        error_message="token=secret",
    )

    assert record_remote_error_metadata(response) == {
        "remote_error_code": "ISV.PARAMETER-ERROR",
        "remote_error_sub_code": "isv.invalid-permission",
    }
    assert consume_remote_error_metadata() == {
        "remote_error_code": "ISV.PARAMETER-ERROR",
        "remote_error_sub_code": "isv.invalid-permission",
    }
    assert consume_remote_error_metadata() == {}


def test_build_durable_unknown_error_keeps_only_safe_machine_codes():
    value = build_durable_unknown_error(
        "placeorder_remote_error",
        {
            "remote_error_code": "ISV.PARAMETER-ERROR",
            "remote_error_sub_code": "isv.invalid-permission",
            "error_message": "token=secret customer@example.com",
        },
    )
    assert value == (
        "placeorder_remote_error"
        "|remote_error_code=ISV.PARAMETER-ERROR"
        "|remote_error_sub_code=isv.invalid-permission"
    )
    assert "secret" not in value
    assert "customer@example.com" not in value


def test_build_durable_unknown_error_drops_unsafe_remote_values():
    value = build_durable_unknown_error(
        "placeorder_remote_error",
        {
            "remote_error_code": "token=super-secret",
            "remote_error_sub_code": "invalid parameter",
        },
    )
    assert value == "placeorder_remote_error"


def test_validate_durable_unknown_error_accepts_only_canonical_safe_shape():
    assert validate_durable_unknown_error("placeorder_timeout") == "placeorder_timeout"
    assert validate_durable_unknown_error(
        "placeorder_remote_error|remote_error_code=400"
    ) == "placeorder_remote_error|remote_error_code=400"
    assert validate_durable_unknown_error(
        "placeorder_remote_error"
        "|remote_error_code=ISV.PARAMETER-ERROR"
        "|remote_error_sub_code=isv.invalid-permission"
    ) == (
        "placeorder_remote_error"
        "|remote_error_code=ISV.PARAMETER-ERROR"
        "|remote_error_sub_code=isv.invalid-permission"
    )


def test_validate_durable_unknown_error_rejects_legacy_free_form_and_tampering():
    unsafe_values = (
        None,
        True,
        "",
        " submission_unknown",
        "submission_unknown",
        "placeorder_remote_error|error_message=token-secret",
        "placeorder_remote_error|remote_error_code=invalid parameter",
        "placeorder_remote_error|remote_error_code=400|remote_error_code=401",
        "placeorder_remote_error|unknown_key=400",
        "placeorder_remote_error|remote_error_sub_code=isv.invalid-permission|remote_error_code=400",
        "placeorder_remote_error|remote_error_code=token=secret",
        "A" * 257,
    )
    for value in unsafe_values:
        assert validate_durable_unknown_error(value) is None


def test_log_event_includes_only_safe_remote_codes(caplog):
    caplog.set_level(logging.INFO)

    log_placeorder_event(
        "provider_unknown",
        outcome_code="placeorder_remote_error",
        duration_ms=123.45,
        remote_order_count=0,
        is_success=False,
        remote_error_code="ISV.PARAMETER-ERROR",
        remote_error_sub_code="isv.invalid-permission",
        error_message="token=super-secret",
        payload="PII",
    )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "remote_error_code=ISV.PARAMETER-ERROR" in text
    assert "remote_error_sub_code=isv.invalid-permission" in text
    assert "placeorder_remote_error" in text
    assert "super-secret" not in text
    assert "error_message" not in text
    assert "payload" not in text
    assert "PII" not in text


def test_log_event_drops_unsafe_remote_code_instead_of_truncating(caplog):
    caplog.set_level(logging.INFO)

    log_placeorder_event(
        "provider_unknown",
        outcome_code="placeorder_remote_error",
        remote_error_code="token=super-secret",
        remote_error_sub_code="invalid parameter",
    )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "remote_error_code=" not in text
    assert "remote_error_sub_code=" not in text
    assert "super-secret" not in text
