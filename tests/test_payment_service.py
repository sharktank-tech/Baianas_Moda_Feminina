from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import Flask

from loja.modules.getway_mercado_pago import payment_service
from loja.modules.getway_mercado_pago.payment_service import (
    PaymentConfigurationError,
    create_preference,
    get_merchant_order,
    money,
    mp_status_to_order_status,
    public_url,
)


def create_test_app():
    app = Flask(__name__)
    return app


def test_money_rounds_to_two_places():
    assert money("10.999") == Decimal("11.00")


def test_flat_shipping_removed_no_free_threshold():
    """Não existe mais calculadora de envio no payment_service: o frete vem de
    cotação por grupo (loja.services.shipping_quote) ou da tarifa comercial
    temporária única (SHIPPING_TEMPORARY_FLAT_RATE)."""
    assert not hasattr(payment_service, "calculate_shipping")


def test_public_url_upgrades_protocol_relative_origin_to_https():
    app = create_test_app()
    app.config["APP_PUBLIC_URL"] = "//www.pollomodas.com.br"

    with app.app_context():
        result = public_url("/pagamentos/webhook")

    assert result == "https://www.pollomodas.com.br/pagamentos/webhook"


def test_public_url_rejects_insecure_non_local_origin():
    app = create_test_app()
    app.config["APP_PUBLIC_URL"] = "http://www.pollomodas.com.br"

    with app.app_context(), pytest.raises(PaymentConfigurationError):
        public_url("/pagamentos/webhook")


def test_public_url_allows_http_for_local_development():
    app = create_test_app()
    app.config["APP_PUBLIC_URL"] = "http://127.0.0.1:5001"

    with app.app_context():
        result = public_url("/pagamentos/webhook")

    assert result == "http://127.0.0.1:5001/pagamentos/webhook"


def test_create_preference_uses_absolute_https_callbacks(monkeypatch):
    app = create_test_app()
    app.config.update(
        APP_PUBLIC_URL="//www.pollomodas.com.br",
        MERCADO_PAGO_USE_SANDBOX=False,
    )

    captured = {}
    response = {
        "id": "pref-test",
        "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-test",
    }

    class FakePreference:
        def create(self, preference_data, request_options):
            captured.update(preference_data)
            return {"status": 201, "response": response}

    class FakeSDK:
        def preference(self):
            return FakePreference()

    monkeypatch.setattr(payment_service, "get_sdk", lambda: FakeSDK())

    order = SimpleNamespace(
        id="order-123",
        customer_name="Teste Comprador",
        customer_email="buyer@testuser.com",
        total=Decimal("99.90"),
        user_id="user-123",
        created_at=datetime(2026, 9, 21, 12, 0, 0),
        mp_preference_expires_at=datetime(2026, 9, 22, 12, 0, 0),
        items=[object()],
    )

    with app.app_context():
        preference = create_preference(order)

    assert preference["checkout_url"] == response["init_point"]
    assert captured["notification_url"] == (
        "https://www.pollomodas.com.br/pagamentos/webhook"
    )
    assert captured["expires"] is True
    assert captured["expiration_date_from"] == "2026-09-21T12:00:00.000+00:00"
    assert captured["expiration_date_to"] == "2026-09-22T12:00:00.000+00:00"
    assert captured["back_urls"] == {
        "success": (
            "https://www.pollomodas.com.br/pagamentos/retorno/sucesso"
            "?order_id=order-123"
        ),
        "pending": (
            "https://www.pollomodas.com.br/pagamentos/retorno/pendente"
            "?order_id=order-123"
        ),
        "failure": (
            "https://www.pollomodas.com.br/pagamentos/retorno/falha"
            "?order_id=order-123"
        ),
    }


def test_create_preference_rejects_missing_persisted_expiry(monkeypatch):
    app = create_test_app()
    app.config["APP_PUBLIC_URL"] = "http://127.0.0.1:5001"
    monkeypatch.setattr(
        payment_service,
        "get_sdk",
        lambda: pytest.fail("gateway must not be called"),
    )
    order = SimpleNamespace(
        id="order-legacy",
        customer_name="Teste",
        customer_email="buyer@testuser.com",
        total=Decimal("10.00"),
        user_id="user-123",
        created_at=datetime(2026, 9, 21, 12, 0, 0),
        mp_preference_expires_at=None,
        items=[object()],
    )

    with app.app_context(), pytest.raises(
        PaymentConfigurationError,
        match="Prazo da preferência",
    ):
        create_preference(order)


def test_create_preference_uses_init_point_even_with_sandbox_flag(monkeypatch):
    app = create_test_app()
    app.config.update(
        APP_PUBLIC_URL="http://127.0.0.1:5001",
        MERCADO_PAGO_USE_SANDBOX=True,
    )

    response = {
        "id": "pref-test",
        "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-test",
        "sandbox_init_point": "https://sandbox.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-test",
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
        customer_name="Teste Comprador",
        customer_email="buyer@testuser.com",
        total=Decimal("99.90"),
        user_id="user-123",
        created_at=datetime(2026, 9, 21, 12, 0, 0),
        mp_preference_expires_at=datetime(2026, 9, 22, 12, 0, 0),
        items=[object()],
    )

    with app.app_context():
        preference = create_preference(order)

    assert preference["checkout_url"] == response["init_point"]


def test_get_merchant_order_uses_provider_resource(monkeypatch):
    app = create_test_app()

    class FakeMerchantOrder:
        def get(self, merchant_order_id):
            assert merchant_order_id == "12345"
            return {
                "status": 200,
                "response": {
                    "id": 12345,
                    "status": "opened",
                    "order_status": "payment_required",
                    "paid_amount": 0,
                    "payments": [],
                },
            }

    class FakeSDK:
        def merchant_order(self):
            return FakeMerchantOrder()

    monkeypatch.setattr(payment_service, "get_sdk", lambda: FakeSDK())

    with app.app_context():
        result = get_merchant_order("12345")

    assert result["id"] == 12345
    assert result["payments"] == []


def test_status_mapping():
    assert mp_status_to_order_status("approved") == "paid"
    assert mp_status_to_order_status("rejected") == "payment_failed"
    assert mp_status_to_order_status("pending") == "pending_payment"
    assert mp_status_to_order_status("refunded") == "refunded"