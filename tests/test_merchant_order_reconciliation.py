from flask import Flask

from loja.modules.getway_mercado_pago import reconciliation


def _app():
    app = Flask(__name__)
    app.config.update(TESTING=True)
    reconciliation.install_merchant_order_reconciliation(app)
    return app


def test_non_merchant_order_webhook_falls_through(monkeypatch):
    app = _app()

    @app.post("/pagamentos/webhook")
    def fallback():
        return {"fallback": True}, 200

    response = app.test_client().post(
        "/pagamentos/webhook?type=payment&data.id=123",
        json={"type": "payment", "data": {"id": "123"}},
    )

    assert response.status_code == 200
    assert response.get_json() == {"fallback": True}


def test_merchant_order_without_payments_returns_200(monkeypatch):
    app = _app()
    calls = []

    monkeypatch.setattr(
        reconciliation,
        "validate_webhook_signature",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        reconciliation,
        "get_merchant_order",
        lambda merchant_order_id: {
            "id": merchant_order_id,
            "status": "opened",
            "order_status": "payment_required",
            "payments": [],
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "_sync_payment_by_id",
        lambda payment_id: calls.append(payment_id),
    )

    response = app.test_client().post(
        "/pagamentos/webhook?topic=merchant_order&id=456",
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert calls == []


def test_topic_merchant_order_reconciles_each_unique_payment(monkeypatch):
    app = _app()
    calls = []

    monkeypatch.setattr(
        reconciliation,
        "validate_webhook_signature",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        reconciliation,
        "get_merchant_order",
        lambda merchant_order_id: {
            "id": merchant_order_id,
            "payments": [
                {"id": 101},
                {"id": 101},
                {"id": 202},
                {},
            ],
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "_sync_payment_by_id",
        lambda payment_id: calls.append(payment_id),
    )

    response = app.test_client().post(
        "/pagamentos/webhook?data.id=789",
        json={
            "type": "topic_merchant_order_wh",
            "data": {"id": "789"},
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert calls == ["101", "202"]


def test_invalid_merchant_order_signature_is_rejected(monkeypatch):
    app = _app()

    def invalid(*args, **kwargs):
        raise reconciliation.InvalidWebhookError("invalid")

    monkeypatch.setattr(
        reconciliation,
        "validate_webhook_signature",
        invalid,
    )

    response = app.test_client().post(
        "/pagamentos/webhook?topic=merchant_order&id=456",
    )

    assert response.status_code == 401
    assert response.get_json() == {"ok": False}
