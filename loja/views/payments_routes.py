import uuid
from decimal import Decimal

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    request,
    url_for,
)
from flask_login import current_user, login_required

from loja import db
from loja.modules.models import Cart, Coupon, Product
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.modules.getway_mercado_pago.payment_service import (
    PaymentConfigurationError,
    PaymentGatewayError,
    calculate_shipping,
    create_preference,
    money,
)


# O nome "payments" precisa ser exatamente este,
# pois o template usa payments.create_mercado_pago_payment.
payments_blueprint = Blueprint(
    "payments",
    __name__,
)


def _parse_quantity(value):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = 1

    return max(1, min(quantity, 99))


def _required_field(field_name, label, max_length):
    value = str(request.form.get(field_name) or "").strip()

    if not value:
        raise ValueError(f"Informe {label}.")

    if len(value) > max_length:
        raise ValueError(f"{label.capitalize()} possui caracteres demais.")

    return value


def _optional_field(field_name, max_length):
    value = str(request.form.get(field_name) or "").strip()

    if not value:
        return None

    return value[:max_length]


def _normalize_coupon_code(value):
    return "".join(
        str(value or "").strip().upper().split()
    )


def _get_customer_data():
    email = _required_field(
        "email",
        "o e-mail",
        160,
    ).lower()

    if "@" not in email:
        raise ValueError("Informe um e-mail válido.")

    cep = "".join(
        character
        for character in _required_field("cep", "o CEP", 12)
        if character.isdigit()
    )

    if len(cep) != 8:
        raise ValueError("Informe um CEP válido com 8 números.")

    state = _required_field(
        "estado",
        "o estado",
        2,
    ).upper()

    if len(state) != 2 or not state.isalpha():
        raise ValueError("Informe a sigla do estado com 2 letras.")

    return {
        "customer_name": _required_field(
            "nome",
            "o nome completo",
            160,
        ),
        "customer_email": email,
        "customer_phone": _optional_field(
            "telefone",
            30,
        ),
        "zip_code": cep,
        "street": _required_field(
            "endereco",
            "o endereço",
            255,
        ),
        "number": _required_field(
            "numero",
            "o número",
            30,
        ),
        "complement": _optional_field(
            "complemento",
            120,
        ),
        "neighborhood": _required_field(
            "bairro",
            "o bairro",
            120,
        ),
        "city": _required_field(
            "cidade",
            "a cidade",
            120,
        ),
        "state": state,
    }


def _get_direct_product_items():
    raw_product_id = str(
        request.form.get("product_id") or ""
    ).strip()

    try:
        product_id = uuid.UUID(raw_product_id)
    except (TypeError, ValueError) as error:
        raise ValueError("Produto inválido.") from error

    product = (
        Product.query
        .filter_by(
            id=product_id,
            is_active=True,
        )
        .with_for_update()
        .first()
    )

    if not product:
        raise ValueError("Produto não encontrado ou indisponível.")

    quantity = _parse_quantity(
        request.form.get("quantity")
    )

    stock = getattr(product, "stock", None)

    if stock is not None and stock < quantity:
        raise ValueError(
            f"Estoque insuficiente para {product.name}."
        )

    selected_size = str(
        request.form.get("selected_size")
        or request.form.get("size")
        or "Único"
    ).strip()[:20]

    unit_price = money(product.price)
    line_total = money(unit_price * quantity)

    items = [
        {
            "product_id": product.id,
            "product_name": product.name,
            "selected_size": selected_size,
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": line_total,
        }
    ]

    return items


def _get_cart_items():
    cart = (
        Cart.query
        .filter_by(user_id=current_user.id)
        .with_for_update()
        .first()
    )

    if not cart or not cart.items:
        raise ValueError("Seu carrinho está vazio.")

    items = []

    for cart_item in cart.items:
        product = cart_item.product

        if not product or not product.is_active:
            raise ValueError(
                "Um produto do carrinho não está mais disponível."
            )

        quantity = _parse_quantity(cart_item.quantity)
        stock = getattr(product, "stock", None)

        if stock is not None and stock < quantity:
            raise ValueError(
                f"Estoque insuficiente para {product.name}."
            )

        unit_price = money(
            cart_item.unit_price
            if cart_item.unit_price is not None
            else product.price
        )

        items.append(
            {
                "product_id": product.id,
                "product_name": product.name,
                "selected_size": (
                    cart_item.selected_size or "Único"
                ),
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": money(
                    unit_price * quantity
                ),
            }
        )

    return items


def _get_coupon(code, subtotal):
    normalized_code = _normalize_coupon_code(code)

    if not normalized_code:
        return None, Decimal("0.00")

    coupon = (
        Coupon.query
        .filter(
            db.func.upper(Coupon.code)
            == normalized_code
        )
        .with_for_update()
        .first()
    )

    if not coupon:
        raise ValueError("Cupom não encontrado.")

    if not coupon.is_available(subtotal):
        raise ValueError(
            "Cupom expirado, indisponível ou inválido para este pedido."
        )

    discount = money(
        coupon.calculate_discount(subtotal)
    )

    return coupon, discount


# ── Criar pagamento no Mercado Pago ─────────────────────────────────────────
@payments_blueprint.route(
    "/pagamentos/mercado-pago",
    methods=["POST"],
)
@login_required
def create_mercado_pago_payment():
    """
    Recebe o formulário do checkout, cria o pedido interno
    e redireciona o cliente para o Checkout Pro.
    """

    try:
        checkout_type = str(
            request.form.get("checkout_type") or ""
        ).strip().lower()

        if checkout_type == "product":
            item_data = _get_direct_product_items()

        elif checkout_type == "cart":
            item_data = _get_cart_items()

        else:
            raise ValueError(
                "Tipo de checkout inválido."
            )

        subtotal = money(
            sum(
                (
                    item["line_total"]
                    for item in item_data
                ),
                Decimal("0.00"),
            )
        )

        coupon, discount = _get_coupon(
            request.form.get("coupon_code"),
            subtotal,
        )

        shipping = calculate_shipping(subtotal)

        total = money(
            max(
                subtotal + shipping - discount,
                Decimal("0.00"),
            )
        )

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
            **_get_customer_data(),
        )

        db.session.add(order)

        for item in item_data:
            order.items.append(
                OrderItem(
                    id=uuid.uuid4(),
                    **item,
                )
            )

        # Faz o INSERT temporário para que o pedido e os itens
        # estejam disponíveis ao criar a preferência.
        db.session.flush()

        preference = create_preference(order)

        order.mp_preference_id = preference[
            "preference_id"
        ]
        order.mp_checkout_url = preference[
            "checkout_url"
        ]

        db.session.commit()

        return redirect(
            preference["checkout_url"]
        )

    except ValueError as error:
        db.session.rollback()

        flash(
            str(error),
            "danger",
        )

    except (
        PaymentConfigurationError,
        PaymentGatewayError,
    ):
        db.session.rollback()

        current_app.logger.exception(
            "Erro ao criar pagamento no Mercado Pago."
        )

        flash(
            "Não foi possível abrir o pagamento. "
            "Verifique os dados e tente novamente.",
            "danger",
        )

    except Exception:
        db.session.rollback()

        current_app.logger.exception(
            "Erro inesperado ao finalizar a compra."
        )

        flash(
            "Ocorreu um erro ao processar seu pedido.",
            "danger",
        )

    return redirect(
        request.referrer
        or url_for("main.home")
    )