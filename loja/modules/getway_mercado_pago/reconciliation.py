from flask import current_app, jsonify, request

from loja import db
from loja.modules.getway_mercado_pago.payment_service import (
    InvalidWebhookError,
    PaymentConfigurationError,
    PaymentGatewayError,
    get_merchant_order,
    validate_webhook_signature,
)
from loja.modules.getway_mercado_pago.webhook_classification import (
    WebhookKind,
    classify_webhook,
)
from loja.observability.mercado_pago import WH_LEGACY_IPN_IGNORED, log_event


MERCHANT_ORDER_EVENT_TYPES = {
    "merchant_order",
    "topic_merchant_order_wh",
}


def _event_type(body):
    return (
        body.get("type")
        or request.args.get("type")
        or request.args.get("topic")
        or ""
    ).strip().lower()


def _merchant_order_id(body):
    return (
        request.args.get("data.id")
        or (body.get("data") or {}).get("id")
        or request.args.get("id")
    )


def _is_legacy_ipn():
    """Identifica somente o formato IPN legado observado em produção.

    IPN legado chega como ?id=<resource>&topic=<topic> e não contém a assinatura
    secreta usada pelos Webhooks atuais. Ele é tratado como sinal sem confiança:
    respondemos 200 para interromper retries, mas NÃO consultamos provedor, NÃO
    sincronizamos Payment e NÃO alteramos estado local. A fonte confiável segue
    sendo o Webhook assinado (?data.id=...&type=...).
    """
    return classify_webhook(request.args) == WebhookKind.LEGACY_IPN


def _sync_payment_by_id(payment_id):
    # Import tardio evita ciclo de import durante create_app().
    from loja.views.payments_routes import _sync_payment

    return _sync_payment(payment_id)


def _payment_ids(merchant_order):
    result = []
    seen = set()

    for payment in merchant_order.get("payments") or []:
        payment_id = payment.get("id") if isinstance(payment, dict) else None
        normalized = str(payment_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)

    return result


def install_merchant_order_reconciliation(app):
    """Reconcilia Webhooks modernos e neutraliza o residual IPN legado.

    O webhook `payment` moderno continua sendo tratado pela rota existente.
    Merchant Order moderna é consultada diretamente no Mercado Pago e seus
    Payments seguem pelo `_sync_payment()` idempotente. Notificações IPN legadas
    são reconhecidas apenas para retornar 200 sem qualquer efeito colateral.
    """
    if getattr(app, "_mp_merchant_order_reconciliation_installed", False):
        return

    app._mp_merchant_order_reconciliation_installed = True

    @app.before_request
    def reconcile_merchant_order_webhook():
        if request.method != "POST" or request.path != "/pagamentos/webhook":
            return None

        if _is_legacy_ipn():
            log_event(
                WH_LEGACY_IPN_IGNORED,
                level="info",
                legacy_topic=(request.args.get("topic") or "").strip().lower(),
            )
            return jsonify({"ok": True}), 200

        body = request.get_json(silent=True) or {}
        event_type = _event_type(body)

        if event_type not in MERCHANT_ORDER_EVENT_TYPES:
            return None

        query_data_id = request.args.get("data.id")

        try:
            validate_webhook_signature(
                request.headers.get("x-signature"),
                request.headers.get("x-request-id"),
                query_data_id,
            )
        except InvalidWebhookError:
            current_app.logger.warning(
                "Webhook Merchant Order Mercado Pago com assinatura inválida."
            )
            return jsonify({"ok": False}), 401
        except PaymentConfigurationError:
            current_app.logger.exception(
                "Webhook Merchant Order Mercado Pago sem configuração segura."
            )
            return jsonify({"ok": False}), 503

        merchant_order_id = _merchant_order_id(body)
        if not merchant_order_id:
            return jsonify({"ok": True}), 200

        try:
            merchant_order = get_merchant_order(merchant_order_id)
            for payment_id in _payment_ids(merchant_order):
                _sync_payment_by_id(payment_id)
        except PaymentGatewayError:
            db.session.rollback()
            current_app.logger.exception(
                "Falha ao reconciliar Merchant Order do Mercado Pago."
            )
            return jsonify({"ok": False}), 500
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Falha inesperada ao reconciliar Merchant Order do Mercado Pago."
            )
            return jsonify({"ok": False}), 500

        return jsonify({"ok": True}), 200
