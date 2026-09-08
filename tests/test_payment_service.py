from decimal import Decimal
from types import SimpleNamespace

from flask import Flask

from loja.modules.getway_mercado_pago import payment_service
from loja.modules.getway_mercado_pago.payment_service import (
    calculate_shipping,
    create_preference,
    money,
    mp_status_to_order_status,
)


def create_test_app():
    app = Flask(__name__)
    app.config.update(
        PAYMENT_FLAT_SHIPPING="18.90",
        FREE_SHIPPING_MIN="249.00",
    )
    return app


def test_money_rounds_to_two_places():
    assert money("10.999") == Decimal("11.00")


def test_shipping_is_free_from_threshold():
    app = create_test_app()
    with app.app_context():
        assert calculate_shipping("249.00") == Decimal("0.00")


def test_shipping_uses_flat_value_below_threshold():
    app = create_test_app()
    with app.app_context():
        assert calculate_shipping("100.00") == Decimal("18.90")


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
        items=[object()],
    )

    with app.app_context():
        preference = create_preference(order)

    assert preference["checkout_url"] == response["init_point"]


def test_status_mapping():
    assert mp_status_to_order_status("approved") == "paid"
    assert mp_status_to_order_status("rejected") == "payment_failed"
    assert mp_status_to_order_status("pending") == "pending_payment"
    assert mp_status_to_order_status("refunded") == "refunded"
