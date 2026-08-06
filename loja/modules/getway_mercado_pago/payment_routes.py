import uuid
from datetime import datetime
from decimal import Decimal

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from loja import db
from loja.modules.models import Cart, Coupon, Product
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem, Payment
from loja.modules.getway_mercado_pago.payment_service import (
    InvalidWebhookError,
    PaymentConfigurationError,
    PaymentGatewayError,
    calculate_shipping,
    create_preference,
    get_payment,
    money,
    mp_status_to_order_status,
    validate_webhook_signature,
)


payments_blueprint = Blueprint("payments", __name__)


def _required_form_text(field, label, max_length):
    value = (request.form.get(field) or "").strip()
    if not value:
        raise ValueError(f"Informe {label}.")
    if len(value) > max_length:
        raise ValueError(f"{label.capitalize()} possui caracteres demais.")
    return value


def _optional_form_text(field, max_length):
    value = (request.form.get(field) or "").strip()
    if not value:
        return None
    return value[:max_length]


def _parse_quantity(value):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = 1
    return min(max(quantity, 1), 99)


def _normalize_coupon_code(value):
    return "".join((value or "").strip().upper().split())


def _cart_order_items():
    cart = (
        Cart.query
        .filter_by(user_id=current_user.id)
        .with_for_update()
        .first()
    )
    if not cart or not cart.items:
        raise ValueError("Seu carrinho está vazio.")

    result = []
    for item in cart.items:
        product = item.product
        if not product or not product.is_active:
            raise ValueError("Um produto do carrinho não está mais disponível.")

        if product.stock is not None and product.stock < item.quantity:
            raise ValueError(
                f"Estoque insuficiente para {product.name}."
            )

        unit_price = money(item.unit_price)
        result.append(
            {
                "product_id": product.id,
                "product_name": product.name,
                "selected_size": item.selected_size,
                "quantity": item.quantity,
                "unit_price": unit_price,
                "line_total": money(unit_price * item.quantity),
            }
        )

    return result, cart


def _product_order_items():
    raw_product_id = (request.form.get("product_id") or "").strip()
    try:
        product_id = uuid.UUID(raw_product_id)
    except (ValueError, TypeError) as error:
        raise ValueError("Produto inválido.") from error

    product = (
        Product.query
        .filter_by(id=product_id, is_active=True)
        .with_for_update()
        .first()
    )
    if not product:
        raise ValueError("Produto não encontrado ou indisponível.")

    quantity = _parse_quantity(request.form.get("quantity"))
    if product.stock is not None and product.stock < quantity:
        raise ValueError("Quantidade maior que o estoque disponível.")

    selected_size = (
        request.form.get("selected_size")
        or request.form.get("size")
        or "Único"
    ).strip()[:20]

    unit_price = money(product.price)
    return [
        {
            "product_id": product.id,
            "product_name": product.name,
            "selected_size": selected_size,
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": money(unit_price * quantity),
        }
    ], None


def _get_coupon(code, subtotal):
    normalized = _normalize_coupon_code(code)
    if not normalized:
        return None, Decimal("0.00")

    coupon = (
        Coupon.query
        .filter(db.func.upper(Coupon.code) == normalized)
        .with_for_update()
        .first()
    )
    if not coupon or not coupon.is_available(subtotal):
        raise ValueError("Cupom inválido, expirado ou indisponível.")

    if coupon.first_purchase_only:
        previous_order = Order.query.filter(
            Order.user_id == current_user.id,
            Order.status.in_(["paid", "manual_review"]),
        ).first()
        if previous_order:
            raise ValueError("Este cupom é exclusivo para a primeira compra.")

    discount = money(coupon.calculate_discount(subtotal))
    return coupon, discount


def _address_data():
    email = _required_form_text("email", "o e-mail", 160).lower()
    if "@" not in email:
        raise ValueError("Informe um e-mail válido.")

    zip_code = "".join(
        char for char in _required_form_text("cep", "o CEP", 12)
        if char.isdigit()
    )
    if len(zip_code) != 8:
        raise ValueError("Informe um CEP com 8 números.")

    state = _required_form_text("estado", "o estado", 2).upper()
    if len(state) != 2 or not state.isalpha():
        raise ValueError("Informe a sigla do estado com 2 letras.")

    return {
        "customer_name": _required_form_text("nome", "o nome completo", 160),
        "customer_email": email,
        "customer_phone": _optional_form_text("telefone", 30),
        "zip_code": zip_code,
        "street": _required_form_text("endereco", "o endereço", 255),
        "number": _required_form_text("numero", "o número", 30),
        "complement": _optional_form_text("complemento", 120),
        "neighborhood": _required_form_text("bairro", "o bairro", 120),
        "city": _required_form_text("cidade", "a cidade", 120),
        "state": state,
    }


@payments_blueprint.route(
    "/pagamentos/mercado-pago",
    methods=["POST"],
)
@login_required
def create_mercado_pago_payment():
    try:
        checkout_type = (
            request.form.get("checkout_type") or "product"
        ).strip().lower()

        if checkout_type == "cart":
            item_data, cart = _cart_order_items()
        elif checkout_type == "product":
            item_data, cart = _product_order_items()
        else:
            raise ValueError("Tipo de checkout inválido.")

        subtotal = money(sum(
            (item["line_total"] for item in item_data),
            Decimal("0.00"),
        ))
        coupon, discount = _get_coupon(
            request.form.get("coupon_code"),
            subtotal,
        )
        shipping = calculate_shipping(subtotal)
        total = money(max(subtotal + shipping - discount, Decimal("0.00")))

        order = Order(
            id=uuid.uuid4(),
            user_id=current_user.id,
            coupon_id=coupon.id if coupon else None,
            coupon_code=coupon.code if coupon else None,
            checkout_type=checkout_type,
            status="pending_payment",
            subtotal=subtotal,
            shipping=shipping,
            discount=discount,
            total=total,
            **_address_data(),
        )
        db.session.add(order)

        for item in item_data:
            order.items.append(OrderItem(id=uuid.uuid4(), **item))

        # Garante que order.items esteja disponível para montar a preferência.
        db.session.flush()

        preference = create_preference(order)
        order.mp_preference_id = preference["preference_id"]
        order.mp_checkout_url = preference["checkout_url"]

        # O carrinho virou um pedido persistido. Em caso de pagamento pendente
        # ou falho, o pedido continua disponível no banco para acompanhamento.
        if cart is not None:
            for cart_item in list(cart.items):
                db.session.delete(cart_item)

        db.session.commit()
        return redirect(preference["checkout_url"])

    except ValueError as error:
        db.session.rollback()
        flash(str(error), "danger")
    except (
        PaymentConfigurationError,
        PaymentGatewayError,
    ) as error:
        db.session.rollback()
        current_app.logger.exception("Erro ao iniciar pagamento")
        flash(
            "Não foi possível abrir o pagamento. Tente novamente.",
            "danger",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Erro inesperado no checkout")
        flash(
            "Ocorreu um erro ao processar seu pedido.",
            "danger",
        )

    return redirect(request.referrer or url_for("main.carrinho"))


def _sync_payment(payment_id):
    mp_payment = get_payment(payment_id)
    external_reference = str(
        mp_payment.get("external_reference") or ""
    ).strip()

    try:
        order_id = uuid.UUID(external_reference)
    except (ValueError, TypeError):
        current_app.logger.warning(
            "Pagamento %s sem external_reference válido.",
            payment_id,
        )
        return None

    order = (
        Order.query
        .filter_by(id=order_id)
        .with_for_update()
        .first()
    )
    if not order:
        current_app.logger.warning(
            "Pedido %s do pagamento %s não encontrado.",
            external_reference,
            payment_id,
        )
        return None

    provider_payment_id = str(mp_payment["id"])
    payment = Payment.query.filter_by(
        provider_payment_id=provider_payment_id
    ).first()

    amount = money(mp_payment.get("transaction_amount"))
    currency = (mp_payment.get("currency_id") or "").upper()
    mp_status = (mp_payment.get("status") or "pending").lower()
    status_detail = mp_payment.get("status_detail")

    if payment is None:
        payment = Payment(
            id=uuid.uuid4(),
            order_id=order.id,
            provider_payment_id=provider_payment_id,
            status=mp_status,
            amount=amount,
            currency=currency or "BRL",
            raw_payload=mp_payment,
        )
        db.session.add(payment)

    payment.status = mp_status
    payment.status_detail = status_detail
    payment.amount = amount
    payment.currency = currency or "BRL"
    payment.payment_method_id = mp_payment.get("payment_method_id")
    payment.payment_type_id = mp_payment.get("payment_type_id")
    payment.raw_payload = mp_payment

    order.mp_payment_id = provider_payment_id
    order.mp_status = mp_status
    order.mp_status_detail = status_detail

    if currency != "BRL" or amount != money(order.total):
        order.status = "manual_review"
        order.manual_review_reason = (
            f"Valor/moeda divergente: recebido {currency} {amount}; "
            f"esperado BRL {money(order.total)}."
        )
        if mp_status == "approved" and order.paid_at is None:
            order.paid_at = datetime.utcnow()
        db.session.commit()
        return order

    mapped_status = mp_status_to_order_status(mp_status)

    if mapped_status == "paid":
        if order.status not in {"paid", "manual_review"}:
            locked_products = {}
            review_reasons = []

            for item in order.items:
                if item.product_id is None:
                    review_reasons.append(
                        f"Produto removido: {item.product_name}."
                    )
                    continue

                product = (
                    Product.query
                    .filter_by(id=item.product_id)
                    .with_for_update()
                    .first()
                )
                locked_products[item.id] = product

                if product is None:
                    review_reasons.append(
                        f"Produto não encontrado: {item.product_name}."
                    )
                elif (
                    product.stock is not None
                    and product.stock < item.quantity
                ):
                    review_reasons.append(
                        f"Estoque insuficiente: {item.product_name}."
                    )

            locked_coupon = None
            if order.coupon_id and order.coupon_counted_at is None:
                locked_coupon = (
                    Coupon.query
                    .filter_by(id=order.coupon_id)
                    .with_for_update()
                    .first()
                )
                if (
                    locked_coupon is not None
                    and locked_coupon.usage_limit is not None
                    and locked_coupon.times_used >= locked_coupon.usage_limit
                ):
                    review_reasons.append(
                        f"Limite do cupom {order.coupon_code} atingido."
                    )

            order.paid_at = order.paid_at or datetime.utcnow()

            if review_reasons:
                order.status = "manual_review"
                order.manual_review_reason = " ".join(review_reasons)
            else:
                for item in order.items:
                    product = locked_products.get(item.id)
                    if product is not None and product.stock is not None:
                        product.stock -= item.quantity

                if locked_coupon is not None:
                    locked_coupon.times_used += 1
                    order.coupon_counted_at = datetime.utcnow()

                order.status = "paid"
                order.manual_review_reason = None

    elif mapped_status in {"refunded", "charged_back"}:
        order.status = mapped_status
    elif order.status not in {"paid", "manual_review", "refunded", "charged_back"}:
        order.status = mapped_status

    db.session.commit()
    return order


@payments_blueprint.route("/pagamentos/webhook", methods=["POST"])
def mercado_pago_webhook():
    body = request.get_json(silent=True) or {}
    query_data_id = request.args.get("data.id")

    try:
        validate_webhook_signature(
            request.headers.get("x-signature"),
            request.headers.get("x-request-id"),
            query_data_id,
        )
    except InvalidWebhookError:
        current_app.logger.warning("Webhook Mercado Pago com assinatura inválida.")
        return jsonify({"ok": False}), 401
    except PaymentConfigurationError:
        current_app.logger.exception("Webhook Mercado Pago sem configuração segura.")
        return jsonify({"ok": False}), 503

    event_type = body.get("type") or request.args.get("type")
    payment_id = (
        query_data_id
        or (body.get("data") or {}).get("id")
        or request.args.get("id")
    )

    if event_type not in {None, "payment"}:
        return jsonify({"ok": True}), 200

    if not payment_id:
        return jsonify({"ok": True}), 200

    try:
        _sync_payment(payment_id)
    except PaymentGatewayError:
        db.session.rollback()
        current_app.logger.exception(
            "Falha ao consultar pagamento %s.",
            payment_id,
        )
        # 500 faz o provedor tentar novamente.
        return jsonify({"ok": False}), 500
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Falha inesperada ao processar webhook %s.",
            payment_id,
        )
        return jsonify({"ok": False}), 500

    return jsonify({"ok": True}), 200


@payments_blueprint.route("/pagamentos/retorno/<result>")
@login_required
def payment_return(result):
    payment_id = (
        request.args.get("payment_id")
        or request.args.get("collection_id")
    )

    if payment_id:
        try:
            _sync_payment(payment_id)
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Não foi possível sincronizar o pagamento no retorno."
            )

    raw_order_id = (
        request.args.get("order_id")
        or request.args.get("external_reference")
    )
    order = None

    try:
        order_uuid = uuid.UUID(str(raw_order_id))
    except (ValueError, TypeError):
        order_uuid = None

    if order_uuid:
        order = Order.query.filter_by(
            id=order_uuid,
            user_id=current_user.id,
        ).first()

    return render_template(
        "payment/result.html",
        result=result,
        order=order,
    )
