from decimal import Decimal

from flask import Flask

from loja.modules.getway_mercado_pago.payment_service import (
    calculate_shipping,
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


def test_status_mapping():
    assert mp_status_to_order_status("approved") == "paid"
    assert mp_status_to_order_status("rejected") == "payment_failed"
    assert mp_status_to_order_status("pending") == "pending_payment"
    assert mp_status_to_order_status("refunded") == "refunded"
