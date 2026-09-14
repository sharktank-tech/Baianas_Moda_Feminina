import logging
import uuid

import pytest

from loja.modules.getway_mercado_pago.webhook_classification import (
    WebhookKind,
    classify_webhook,
)
from loja.observability.mercado_pago import (
    WH_LEGACY_IPN_IGNORED,
    WH_MALFORMED_IGNORED,
    WH_MODERN_REJECTED,
    WH_MODERN_VALIDATED,
    stable_hash,
)


def _query_args(pairs):
    from werkzeug.datastructures import MultiDict

    args = MultiDict()
    for key, value in pairs:
        args.add(key, value)
    return args


# ── Classificação pura ──────────────────────────────────────────────────────

def test_classifies_modern_when_data_id_present_in_query():
    kind = classify_webhook(_query_args([("data.id", "789"), ("type", "payment")]))
    assert kind == WebhookKind.MODERN


def test_classifies_modern_even_when_legacy_topic_mixed_in():
    # Regra: data.id presente no query NUNCA cai no caminho legado.
    kind = classify_webhook(_query_args([("data.id", "789"), ("topic", "payment")]))
    assert kind == WebhookKind.MODERN


def test_classifies_legacy_ipn_payment():
    kind = classify_webhook(_query_args([("id", "178286687710"), ("topic", "payment")]))
    assert kind == WebhookKind.LEGACY_IPN


def test_classifies_legacy_ipn_merchant_order():
    kind = classify_webhook(_query_args([("id", "44327435803"), ("topic", "merchant_order")]))
    assert kind == WebhookKind.LEGACY_IPN


def test_classifies_unknown_topic_as_malformed():
    kind = classify_webhook(_query_args([("id", "5555"), ("topic", "other")]))
    assert kind == WebhookKind.MALFORMED


def test_classifies_missing_resource_id_as_malformed():
    kind = classify_webhook(_query_args([("topic", "payment")]))
    assert kind == WebhookKind.MALFORMED


def test_classifies_type_without_data_id_as_malformed():
    # `type` sem `data.id` no query é uma forma parcial moderna — não é IPN
    # legado confiável e também não é um webhook moderno validável.
    kind = classify_webhook(_query_args([("id", "123"), ("type", "payment")]))
    assert kind == WebhookKind.MALFORMED


def test_classifies_empty_query_as_malformed():
    assert classify_webhook(_query_args([])) == WebhookKind.MALFORMED


def test_does_not_infer_modern_from_body_only():
    # "Não inferir um formato moderno apenas porque existe um id no corpo."
    kind = classify_webhook(_query_args([]), body={"type": "payment", "data": {"id": "123"}})
    assert kind == WebhookKind.MALFORMED


def test_topic_and_id_required_for_legacy_ipn():
    assert classify_webhook(_query_args([("id", "123")])) == WebhookKind.MALFORMED
    assert classify_webhook(_query_args([("topic", "payment")])) == WebhookKind.MALFORMED


# ── Comportamento no app real ───────────────────────────────────────────────


def _count_webhook_mutations():
    from loja.modules.getway_mercado_pago.payment_models import Order, Payment

    return {"orders": Order.query.count(), "payments": Payment.query.count()}


def test_legacy_ipn_payment_ignored_without_provider_or_db(client, monkeypatch, make_product, caplog):
    caplog.set_level(logging.INFO)
    from loja.views import payments_routes

    product = make_product()
    db_before = _count_webhook_mutations()

    provider_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: provider_calls.append(("sync", payment_id)),
    )

    resp = client.post("/pagamentos/webhook?id=178286687710&topic=payment")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert provider_calls == []

    from loja.modules.models import Product
    from loja import db

    refreshed = db.session.get(Product, product.id)
    assert refreshed.stock == product.stock
    assert _count_webhook_mutations() == db_before

    assert WH_LEGACY_IPN_IGNORED in caplog.text


def test_legacy_ipn_merchant_order_ignored_without_provider(client, monkeypatch):
    from loja.views import payments_routes

    provider_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: provider_calls.append(("sync", payment_id)),
    )

    resp = client.post("/pagamentos/webhook?id=44327435803&topic=merchant_order")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert provider_calls == []


def test_legacy_ipn_does_not_trigger_modern_signature_validation(client, monkeypatch):
    from loja.views import payments_routes

    def fail_if_called(*args, **kwargs):
        raise AssertionError("assinatura não deve ser validada para IPN legado")

    monkeypatch.setattr(payments_routes, "validate_webhook_signature", fail_if_called)

    resp = client.post("/pagamentos/webhook?id=178286687710&topic=payment")
    assert resp.status_code == 200


def test_modern_payment_with_valid_signature_still_syncs(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    from loja.views import payments_routes

    sync_calls = []
    monkeypatch.setattr(payments_routes, "validate_webhook_signature", lambda *a, **k: None)
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: sync_calls.append(payment_id),
    )

    resp = client.post(
        "/pagamentos/webhook?data.id=123&type=payment",
        json={"type": "payment", "data": {"id": "123"}},
    )

    assert resp.status_code == 200
    assert sync_calls == ["123"]
    assert WH_MODERN_VALIDATED in caplog.text


def test_modern_payment_with_invalid_signature_rejected_without_sync(client, monkeypatch):
    from loja.views import payments_routes

    def invalid(*args, **kwargs):
        raise payments_routes.InvalidWebhookError("invalid")

    monkeypatch.setattr(payments_routes, "validate_webhook_signature", invalid)
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: (_ for _ in ()).throw(
            AssertionError("não deve sincronizar com assinatura inválida")
        ),
    )

    resp = client.post(
        "/pagamentos/webhook?data.id=123&type=payment",
        json={"type": "payment", "data": {"id": "123"}},
    )

    assert resp.status_code == 401
    assert resp.get_json() == {"ok": False}


def test_modern_payment_without_secret_rejected_503(client, monkeypatch):
    # Sem MERCADO_PAGO_WEBHOOK_SECRET configurado, o webhook moderno
    # continua fail-closed (503) — nunca processa sem assinatura.
    app = client.application
    app.config["MERCADO_PAGO_WEBHOOK_SECRET"] = ""

    from loja.views import payments_routes

    sync_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: sync_calls.append(payment_id),
    )

    resp = client.post(
        "/pagamentos/webhook?data.id=123&type=payment",
        json={"type": "payment", "data": {"id": "123"}},
    )

    assert resp.status_code == 503
    assert resp.get_json() == {"ok": False}
    assert sync_calls == []


def test_mixed_data_id_and_legacy_topic_is_modern_and_fail_closed(client, monkeypatch):
    # data.id presente ainda que viaja com topic legado: precisa de assinatura.
    app = client.application
    app.config["MERCADO_PAGO_WEBHOOK_SECRET"] = ""

    resp = client.post("/pagamentos/webhook?data.id=123&topic=payment")
    assert resp.status_code == 503
    assert resp.get_json() == {"ok": False}


def test_unknown_topic_ignored_200_not_503(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    # Regressão: `?id=...&topic=other` caía na validação moderna e virava
    # 401/503. Agora é malformado/desconhecido → 200 sem efeitos colaterais.
    from loja.views import payments_routes

    sync_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: sync_calls.append(payment_id),
    )

    resp = client.post("/pagamentos/webhook?id=5555&topic=other")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert sync_calls == []
    assert WH_MALFORMED_IGNORED in caplog.text


def test_missing_resource_id_ignored_200(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    from loja.views import payments_routes

    sync_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: sync_calls.append(payment_id),
    )

    resp = client.post("/pagamentos/webhook?topic=payment")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert sync_calls == []
    assert WH_MALFORMED_IGNORED in caplog.text


def test_body_only_payload_ignored_200_without_processing(client, monkeypatch, caplog):
    # Um id no corpo NÃO torna a notificação moderna: sem query data.id ela é
    # malformada e segue seguramente ignorada (200), nunca 401/503.
    caplog.set_level(logging.INFO)
    from loja.views import payments_routes

    sync_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda payment_id: sync_calls.append(payment_id),
    )

    resp = client.post(
        "/pagamentos/webhook",
        json={"type": "payment", "data": {"id": "123"}},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert sync_calls == []
    assert WH_MALFORMED_IGNORED in caplog.text


# ── Telemetria sanitizada ───────────────────────────────────────────────────


def test_legacy_ignored_event_has_no_plaintext_identifiers(client, caplog):
    caplog.set_level(logging.INFO)
    client.post("/pagamentos/webhook?id=178286687710&topic=payment")

    assert WH_LEGACY_IPN_IGNORED in caplog.text
    assert "178286687710" not in caplog.text
    assert stable_hash("178286687710") not in caplog.text


def test_malformed_ignored_event_has_no_payload(client, caplog):
    caplog.set_level(logging.INFO)
    client.post("/pagamentos/webhook?id=5555&topic=other")

    assert WH_MALFORMED_IGNORED in caplog.text
    assert "5555" not in caplog.text


def test_modern_rejected_event_logged_for_invalid_signature(client, monkeypatch, caplog):
    from loja.views import payments_routes

    def invalid(*args, **kwargs):
        raise payments_routes.InvalidWebhookError("invalid")

    monkeypatch.setattr(payments_routes, "validate_webhook_signature", invalid)

    caplog.set_level(logging.INFO)
    resp = client.post("/pagamentos/webhook?data.id=123&type=payment")
    assert resp.status_code == 401

    assert WH_MODERN_REJECTED in caplog.text
    # Sem assinatura, x-request-id ou payload em texto puro.
    assert "x-signature" not in caplog.text
    assert "x-request-id" not in caplog.text