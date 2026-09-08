from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin

import mercadopago
from flask import current_app
from mercadopago.config import RequestOptions
from mercadopago.webhook import (
    InvalidWebhookSignatureError,
    WebhookSignatureValidator,
)


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


def public_url(path):
    base_url = (current_app.config.get("APP_PUBLIC_URL") or "").strip().rstrip("/")
    if not base_url:
        raise PaymentConfigurationError("APP_PUBLIC_URL não foi configurada.")
    return urljoin(base_url + "/", path.lstrip("/"))


def calculate_shipping(subtotal):
    subtotal = money(subtotal)
    flat = money(current_app.config.get("PAYMENT_FLAT_SHIPPING", "18.90"))
    free_min = money(current_app.config.get("FREE_SHIPPING_MIN", "249.00"))

    if free_min > 0 and subtotal >= free_min:
        return Decimal("0.00")
    return flat


def create_preference(order):
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
        "expires": False,
    }

    request_options = RequestOptions()
    request_options.custom_headers = {
        "x-idempotency-key": f"preference-{order.id}",
    }

    result = sdk.preference().create(preference_data, request_options)
    status_code = int(result.get("status") or 0)
    response = result.get("response") or {}

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
    result = get_sdk().payment().get(str(payment_id))
    status_code = int(result.get("status") or 0)
    response = result.get("response") or {}

    if status_code != 200 or not response.get("id"):
        message = response.get("message") or "Pagamento não encontrado."
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
