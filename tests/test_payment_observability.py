import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from flask import Flask

from loja.modules.getway_mercado_pago import payment_service
from loja.observability.mercado_pago import sanitize_fields, stable_hash


def test_stable_hash_is_deterministic_and_non_plaintext():
    value = "sensitive-provider-id"
    digest = stable_hash(value)
    assert digest == stable_hash(value)
    assert digest != value
    assert len(digest) == 16


def test_sanitize_fields_redacts_secrets_and_hashes_ids():
    sanitized = sanitize_fields(
        {
            "access_token": "secret-token",
            "email": "buyer@example.com",
            "merchant_order_id": "123456",
            "external_reference": "order-123",
            "status": "opened",
        }
    )

    assert sanitized["access_token"] == "[REDACTED]"
    assert sanitized["email"] == "[REDACTED]"
    assert sanitized["merchant_order_id"] == stable_hash("123456")
    assert sanitized["external_reference"] == stable_hash("order-123")
    assert sanitized["status"] == "opened"


def test_create_preference_emits_sanitized_provider_telemetry(monkeypatch, caplog):
    app = Flask(__name__)
    app.config.update(
        APP_PUBLIC_URL="https://example.test",
    )

    response = {
        "id": "pref-live-123",
        "collector_id": "collector-456",
        "site_id": "MLB",
        "external_reference": "order-123",
        "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-live-123",
    }

    class FakePreference:
        def create(self, preference_data, request_options):
            return {"status": 201, "response": response}

    class FakeSDK:
        def preference(self):
            return FakePreference()

    monkeypatch.setattr(payment_service, "get_sdk", lambda: FakeSDK())

    order = SimpleNamespace(
        id="order-123",
        customer_name="Buyer Example",
        customer_email="buyer@example.com",
        total=Decimal("19.60"),
        user_id="user-123",
        created_at=datetime(2026, 9, 21, 12, 0, 0),
        mp_preference_expires_at=datetime(2026, 9, 22, 12, 0, 0),
        items=[object()],
    )

    caplog.set_level(logging.INFO)
    with app.test_request_context("/pagamentos/mercado-pago", method="POST"):
        result = payment_service.create_preference(order)

    assert result["preference_id"] == "pref-live-123"
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "provider.preference.create.requested" in logs
    assert "provider.preference.create.completed" in logs
    assert "buyer@example.com" not in logs
    assert "pref-live-123" not in logs
    assert stable_hash("pref-live-123") in logs
    assert stable_hash("order-123") in logs
