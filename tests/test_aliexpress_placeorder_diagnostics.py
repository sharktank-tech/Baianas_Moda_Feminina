from __future__ import annotations

from dataclasses import dataclass

from loja.integrations.aliexpress.placeorder_diagnostics import (
    log_placeorder_event,
    request_shape,
    sanitize_unknown_code,
)


@dataclass(frozen=True)
class _Item:
    logistics_service_name: str | None = None


@dataclass(frozen=True)
class _Request:
    shipping_address: dict
    items: tuple


def test_sanitize_unknown_code_allowlist():
    assert sanitize_unknown_code("placeorder_timeout") == "placeorder_timeout"
    assert sanitize_unknown_code("placeorder_transport_error") == "placeorder_transport_error"
    assert sanitize_unknown_code("evil: token=secret") == "placeorder_unexpected_error"


def test_request_shape_contains_only_shape_metadata():
    request = _Request(
        shipping_address={
            "address": "Rua Secreta 1",
            "city": "Cidade",
            "contact_person": "Pessoa Privada",
            "country": "BR",
            "mobile_no": "11999999999",
            "province": "SP",
            "zip": "00000-000",
        },
        items=(_Item(),),
    )

    shape = request_shape(request)

    assert shape == {
        "item_count": 1,
        "shipping_required_present": 7,
        "shipping_required_total": 7,
        "has_logistics_service": False,
    }
    rendered = repr(shape)
    assert "Rua Secreta" not in rendered
    assert "Pessoa Privada" not in rendered
    assert "11999999999" not in rendered


def test_log_event_redacts_free_form_strings(caplog):
    caplog.set_level("INFO")
    log_placeorder_event(
        "provider_unknown",
        outcome_code="token=super-secret",
        duration_ms=123.4,
        item_count=1,
        remote_order_count=0,
    )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret" not in text
    assert "placeorder_unexpected_error" in text
    assert "duration_ms=123.4" in text
