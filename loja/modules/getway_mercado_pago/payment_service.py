from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin, urlsplit

import mercadopago
from flask import current_app
from mercadopago.config import RequestOptions
from mercadopago.webhook import (
    InvalidWebhookSignatureError,
    WebhookSignatureValidator,
)

from loja.observability.mercado_pago import log_event
from loja.services.reservation_policy import as_utc_iso


MONEY_PLACES = Decimal("0.01")


class PaymentConfigurationError(RuntimeError):
    pass


class PaymentGatewayError(RuntimeError):
    pass


class InvalidWebhookError(RuntimeError):
    pass


def money(value):
    return Decimal(str(value or 0)).quantize(
        MONEY_PLACES,
        rounding=ROUND_HALF_UP,
    )


def get_sdk():
    access_token = (current_app.config.get("MERCADO_PAGO_ACCESS_TOKEN") or "").strip()
    if not access_token:
        raise PaymentConfigurationError(
            "MERCADO_PAGO_ACCESS_TOKEN não foi configurado."
        )
    return mercadopago.SDK(access_token)


def _public_origin():
    """Retorna uma origem pública absoluta e segura para callbacks.

    Corrige configurações legadas protocol-relative (//host) para HTTPS e
    rejeita valores sem host, com path/query/fragment ou HTTP fora de ambiente
    local. Assim nenhuma preference de produção volta a enviar back_urls ou
    notification_url relativas ao Mercado Pago.
    """
    raw = (current_app.config.get("APP_PUBLIC_URL") or "").strip().rstrip("/")
    if not raw:
        raise PaymentConfigurationError("APP_PUBLIC_URL não foi configurada.")

    if raw.startswith("//"):
        raw = "https:" + raw

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PaymentConfigurationError(
            "APP_PUBLIC_URL deve ser uma URL absoluta com http:// ou https://."
        )

    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PaymentConfigurationError(
            "APP_PUBLIC_URL deve conter somente a origem pública da loja."
        )

    hostname = (parsed.hostname or "").lower()
    is_local = hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not is_local:
        raise PaymentConfigurationError(
            "APP_PUBLIC_URL pública deve usar https://."
        )

    return f"{parsed.scheme}://{parsed.netloc}"


def public_url(path):
    return urljoin(_public_origin() + "/", str(path or "").lstrip("/"))


def create_preference(order):
    created_at = getattr(order, "created_at", None)
    expires_at = getattr(order, "mp_preference_expires_at", None)
    try:
        expiration_date_from = as_utc_iso(created_at)
        expiration_date_to = as_utc_iso(expires_at)
    except ValueError as error:
        raise PaymentConfigurationError(
            "Prazo da preferência de pagamento não foi definido."
        ) from error
    if expires_at <= created_at:
        raise PaymentConfigurationError(
            "Prazo da preferência de pagamento é inválido."
        )

    sdk = get_sdk()
    name_parts = (order.customer_name or "").strip().split(maxsplit=1)
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    preference_data = {
        # Um item consolidado garante que subtotal - desconto + frete
        # seja exatamente igual ao valor cobrado.
        "items": [
            {
                "id": str(order.id),
                "title": f"Pedido Pollo Modas {str(order.id)[:8]}",
                "description": f"{len(order.items)} item(ns) do pedido",
                "currency_id": "BRL",
                "quantity": 1,
                "unit_price": float(money(order.total)),
            }
        ],
        "payer": {
            "name": first_name,
            "surname": last_name,
            "email": order.customer_email,
        },
        "external_reference": str(order.id),
        "metadata": {
            "order_id": str(order.id),
            "user_id": str(order.user_id),
        },
        "back_urls": {
            "success": public_url(
                f"/pagamentos/retorno/sucesso?order_id={order.id}"
            ),
            "pending": public_url(
                f"/pagamentos/retorno/pendente?order_id={order.id}"
            ),
            "failure": public_url(
                f"/pagamentos/retorno/falha?order_id={order.id}"
            ),
        },
        "notification_url": public_url("/pagamentos/webhook"),
        "auto_return": "approved",
        "statement_descriptor": "POLLO MODAS",
        "expires": True,
        "expiration_date_from": expiration_date_from,
        "expiration_date_to": expiration_date_to,
    }

    request_options = RequestOptions()
    request_options.custom_headers = {
        "x-idempotency-key": f"preference-{order.id}",
    }

    log_event(
        "provider.preference.create.requested",
        order_id=order.id,
        external_reference=order.id,
        amount=money(order.total),
        currency="BRL",
        item_count=len(order.items),
        notification_url_configured=bool(preference_data.get("notification_url")),
        back_urls_configured=all(preference_data.get("back_urls", {}).values()),
    )

    result = sdk.preference().create(preference_data, request_options)
    status_code = int(result.get("status") or 0)
    response = result.get("response") or {}

    log_event(
        "provider.preference.create.completed",
        status_code=status_code,
        order_id=order.id,
        external_reference=response.get("external_reference") or order.id,
        preference_id=response.get("id"),
        collector_id=response.get("collector_id"),
        site_id=response.get("site_id"),
        has_init_point=bool(response.get("init_point")),
        provider_message=response.get("message"),
    )

    if status_code not in {200, 201} or not response.get("id"):
        message = response.get("message") or "Falha ao criar preferência."
        raise PaymentGatewayError(
            f"Mercado Pago retornou HTTP {status_code}: {message}"
        )

    checkout_url = response.get("init_point")

    if not checkout_url:
        raise PaymentGatewayError(
            "O Mercado Pago não retornou a URL do checkout."
        )

    return {
        "preference_id": str(response["id"]),
        "checkout_url": checkout_url,
        "raw": response,
    }


def get_payment(payment_id):
    log_event(
        "provider.payment.get.requested",
        payment_id=payment_id,
    )

    result = get_sdk().payment().get(str(payment_id))
    status_code = int(result.get("status") or 0)
    response = result.get("response") or {}

    log_event(
        "provider.payment.get.completed",
        status_code=status_code,
        payment_id=response.get("id") or payment_id,
        external_reference=response.get("external_reference"),
        payment_status=response.get("status"),
        status_detail=response.get("status_detail"),
        payment_method_id=response.get("payment_method_id"),
        payment_type_id=response.get("payment_type_id"),
        transaction_amount=response.get("transaction_amount"),
        currency=response.get("currency_id"),
        provider_message=response.get("message"),
    )

    if status_code != 200 or not response.get("id"):
        message = response.get("message") or "Pagamento não encontrado."
        raise PaymentGatewayError(
            f"Mercado Pago retornou HTTP {status_code}: {message}"
        )

    return response


def get_merchant_order(merchant_order_id):
    """Consulta a Merchant Order no provedor antes de reconciliar payments."""
    log_event(
        "provider.merchant_order.get.requested",
        merchant_order_id=merchant_order_id,
    )

    result = get_sdk().merchant_order().get(str(merchant_order_id))
    status_code = int(result.get("status") or 0)
    response = result.get("response") or {}
    payments = response.get("payments") or []

    log_event(
        "provider.merchant_order.get.completed",
        status_code=status_code,
        merchant_order_id=response.get("id") or merchant_order_id,
        external_reference=response.get("external_reference"),
        order_status=response.get("order_status"),
        merchant_order_status=response.get("status"),
        paid_amount=response.get("paid_amount"),
        payment_count=len(payments),
        provider_message=response.get("message"),
    )

    if status_code != 200 or not response.get("id"):
        message = response.get("message") or "Merchant Order não encontrada."
        raise PaymentGatewayError(
            f"Mercado Pago retornou HTTP {status_code}: {message}"
        )

    return response


def validate_webhook_signature(x_signature, x_request_id, data_id):
    secret = (
        current_app.config.get("MERCADO_PAGO_WEBHOOK_SECRET") or ""
    ).strip()
    require_signature = bool(
        current_app.config.get(
            "MERCADO_PAGO_REQUIRE_WEBHOOK_SIGNATURE",
            True,
        )
    )

    if not secret:
        if require_signature:
            raise PaymentConfigurationError(
                "MERCADO_PAGO_WEBHOOK_SECRET não foi configurado."
            )
        return

    try:
        WebhookSignatureValidator.validate(
            x_signature,
            x_request_id,
            data_id,
            secret,
        )
    except InvalidWebhookSignatureError as error:
        raise InvalidWebhookError(
            "Assinatura do webhook inválida."
        ) from error


def mp_status_to_order_status(mp_status):
    return {
        "approved": "paid",
        "authorized": "pending_payment",
        "in_process": "pending_payment",
        "in_mediation": "pending_payment",
        "pending": "pending_payment",
        "rejected": "payment_failed",
        "cancelled": "payment_failed",
        "refunded": "refunded",
        "charged_back": "charged_back",
    }.get((mp_status or "").strip().lower(), "pending_payment")