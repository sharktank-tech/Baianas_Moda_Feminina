import uuid
from datetime import timedelta
from decimal import Decimal

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from loja import db, csrf
from loja.utils import utcnow
from loja.modules.models import (
    Cart,
    Coupon,
    Product,
    ProductVariant,
)
from loja.modules.getway_mercado_pago.payment_models import (
    Order,
    OrderItem,
    Payment,
)
from loja.modules.getway_mercado_pago.payment_service import (
    InvalidWebhookError,
    PaymentConfigurationError,
    PaymentGatewayError,
    create_preference,
    get_payment,
    money,
    mp_status_to_order_status,
    validate_webhook_signature,
)
from loja.services.shipping_quote import (
    QuoteUnavailable,
    quote_for_items,
    require_aliexpress_evidence,
    temporary_flat_rate,
)
from loja.services.reservation_policy import reservation_deadline
from loja.services.variant_attributes import (
    VARIANT_LABEL_MAX_LEN,
    variant_dim_value,
)


def _unavailable_message(code: str) -> str:
    return {
        "missing_origin": (
            "Frete indisponível para este destino no momento. "
            "Confira o CEP e tente novamente."
        ),
        "provider_unavailable": (
            "Frete indisponível para este destino no momento. "
            "Confira o CEP e tente novamente."
        ),
        "no_supplier_data": (
            "Frete indisponível para este destino no momento. "
            "Confira o CEP e tente novamente."
        ),
        "currency_mismatch": (
            "Frete indisponível para este destino no momento. "
            "Confira o CEP e tente novamente."
        ),
        "supplier_taxes_unverified": "Frete indisponível: falta confirmar o custo completo para este produto e destino.",
        "supplier_cost_changed": "Os custos ou o preço foram atualizados. Atualize o carrinho e calcule o frete novamente.",
        "acquisition_quantity_unverified": "A cotação disponível cobre uma unidade deste produto. Ajuste a quantidade ou entre em contato.",
        "invalid_destination": "Informe um CEP válido com 8 números.",
        "expired": (
            "Cotação de frete vencida. Calcule novamente e tente outra vez."
        ),
        "invalid_quote": (
            "Frete desatualizado. Calcule novamente e tente outra vez."
        ),
    }.get(
        code,
        "Frete indisponível para este destino no momento. "
        "Confira o CEP e tente novamente.",
    )


def _is_grouped_quote_enabled() -> bool:
    from flask import current_app

    return bool(current_app.config.get("SHIPPING_GROUPED_QUOTE_ENABLED"))


def quote_checkout_shipping(item_data, dest_cep):
    """Frete total do checkout.

    Com flag desligada (SHIPPING_GROUPED_QUOTE_ENABLED=False), cobra a tarifa
    comercial temporária única (temporary_flat_rate) — regra assumida pela
    loja, não cotação por CEP. Com flag ligada, usa cotação por grupo
    (nunca tarifa presumida). Configuração inválida falha sanitizada.

    ITEM 5.5: em QUALQUER modo, linhas AliExpress exigem evidência de frete
    vigente com escopo válido (``require_aliexpress_evidence``) — sem ela a
    venda bloqueia em vez de cobrar o flat. O modo define só o valor.
    """
    if not _is_grouped_quote_enabled():
        try:
            require_aliexpress_evidence(item_data, dest_cep)
            return temporary_flat_rate()
        except QuoteUnavailable as exc:
            raise ValueError(_unavailable_message(exc.code)) from exc

    try:
        result = quote_for_items(item_data, dest_cep)
    except QuoteUnavailable as exc:
        raise ValueError(_unavailable_message(exc.code)) from exc
    return result["total"]


def _require_confirmed_quote(item_data, dest_cep, shipping):
    """Exige confirmação explícita do frete exibido antes da preferência.

    A confirmação é a cotação registrada na sessão (assinada) pelo
    /calcular_frete: compara CEP, itens/quantidades, valor e validade com
    o recálculo autoritativo (banco + CEP do formulário). Qualquer
    divergência, ausência ou vencimento exige nova confirmação — nunca se
    cobra em cima de inputs do navegador.
    """
    from datetime import datetime

    from loja.services.shipping_quote import canonical_items
    from loja.utils import utcnow

    stored = session.get("shipping_quote")
    if not isinstance(stored, dict) or not stored:
        raise ValueError(
            "Calcule o frete e confirme o valor antes de concluir."
        )
    # Cada remessa tem sua validade: basta uma vencer para exigir nova cotação.
    expiries = [stored.get("expires_at")]
    groups = stored.get("groups") or []
    if not isinstance(groups, list):
        raise ValueError(
            "Cotação de frete vencida. Calcule novamente e tente outra vez."
        )
    for group in groups:
        expiries.append(group.get("expires_at") if isinstance(group, dict) else None)
    try:
        deadlines = [datetime.fromisoformat(exp or "") for exp in expiries]
    except ValueError:
        raise ValueError(
            "Cotação de frete vencida. Calcule novamente e tente outra vez."
        ) from None
    if min(deadlines) <= utcnow():
        raise ValueError(
            "Cotação de frete vencida. Calcule novamente e tente outra vez."
        )
    if stored.get("cep") != dest_cep:
        raise ValueError(
            "O frete foi atualizado. Confira o novo valor e confirme novamente."
        )
    if stored.get("items") != [
        {"product_id": pid, "variant_id": vid or None, "quantity": qty}
        for pid, vid, qty in canonical_items(item_data)
    ]:
        raise ValueError(
            "O frete foi atualizado. Confira o novo valor e confirme novamente."
        )
    try:
        confirmed = money(stored.get("shipping"))
    except Exception:
        raise ValueError(
            "O frete foi atualizado. Confira o novo valor e confirme novamente."
        ) from None
    if confirmed != money(shipping):
        raise ValueError(
            "O frete foi atualizado. Confira o novo valor e confirme novamente."
        )
from loja.integrations.aliexpress import (
    ensure_aliexpress_fulfillment,
    cancel_fulfillment_before_submission,
)
from loja.services.acquisition_source import (
    acquisition_to_order_kwargs,
    order_acquisition,
)
from loja.modules.getway_mercado_pago.webhook_classification import (
    WebhookKind,
    classify_webhook,
)
from loja.observability.mercado_pago import (
    WH_LEGACY_IPN_IGNORED,
    WH_MALFORMED_IGNORED,
    WH_MODERN_REJECTED,
    WH_MODERN_VALIDATED,
    log_event,
)


# O nome "payments" precisa ser exatamente este,
# pois o template usa payments.create_mercado_pago_payment.
payments_blueprint = Blueprint(
    "payments",
    __name__,
)


def _safe_referrer(value):
    """Aceita somente caminhos internos, evitando redirecionamento externo."""
    return bool(
        value
        and value.startswith("/")
        and not value.startswith("//")
    )


IDEMPOTENCY_KEY_MAX_LEN = 64


def _normalize_idempotency_key(raw):
    """Chave de idempotência enviada pelo form de checkout (opcional).

    Aceita 8–64 caracteres [A-Za-z0-9_-]. Qualquer outro valor é descartado
    (None): entrada inválida não pode travar o checkout. A proteção real é a
    constraint UNIQUE (user_id, idempotency_key) no banco.
    """
    key = str(raw or "").strip()

    if not 8 <= len(key) <= IDEMPOTENCY_KEY_MAX_LEN:
        return None

    if any(not (ch.isalnum() or ch in "-_") for ch in key):
        return None

    return key


def _find_duplicate_checkout(idempotency_key):
    """Pedido já criado com esta chave pelo mesmo usuário, se houver."""
    if not idempotency_key:
        return None

    return (
        Order.query
        .filter(
            Order.user_id == current_user.id,
            Order.idempotency_key == idempotency_key,
        )
        .first()
    )


def _redirect_to_existing_order(order):
    """Reaproveita o pedido do reenvio: leva o cliente ao pagamento dele.

    Sem mensagem nova ao cliente: o destino é a mesma página do Mercado Pago
    que o fluxo normal mostraria.
    """
    target = order.mp_checkout_url

    if not target:
        target = url_for(
            "main.detalhes_encomenda",
            order_id=order.id,
        )

    return redirect(target)


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


def _product_uses_variants(product):
    """Um produto usa variantes quando vem do AliExpress ou possui variantes
    ativas com estoque. Mesma semântica do storefront (routes.py)."""
    variants = getattr(product, "variants", None) or []

    return (
        getattr(product, "source", None) == "aliexpress"
        or any(
            getattr(v, "is_active", False)
            and int(getattr(v, "stock", 0) or 0) > 0
            for v in variants
        )
    )


def _resolve_payment_variant(product, raw_variant_id, fallback_size):
    """Resolve e valida a variante selecionada para o checkout.

    Produtos que usam variantes exigem uma variante válida (ativa, com
    estoque > 0). Retorna (variant, size_label). Nunca expõe
    supplier_sku_id/supplier_product_id.
    """
    if not _product_uses_variants(product):
        return None, str(fallback_size or "Único").strip()[:20]

    if not raw_variant_id:
        raise ValueError("Selecione uma variação do produto.")

    try:
        variant_uuid = uuid.UUID(str(raw_variant_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("A variação selecionada é inválida.")

    variant = (
        ProductVariant.query
        .filter(
            ProductVariant.id == variant_uuid,
            ProductVariant.product_id == product.id,
            ProductVariant.is_active.is_(True),
        )
        .first()
    )

    if variant is None:
        raise ValueError("A variação selecionada não está mais disponível.")

    if int(variant.stock or 0) <= 0:
        raise ValueError("Esta variação está temporariamente indisponível.")

    # Snapshot do tamanho COMPLETO resolvido (coluna ou attributes). Acima do
    # teto da coluna (migração 0028), falha explícita: nunca truncamento
    # silencioso. A identidade continua sendo o variant_id.
    label = variant_dim_value(variant, "size") or "UNICO"
    if len(label) > VARIANT_LABEL_MAX_LEN:
        raise ValueError("O tamanho desta variação excede o limite suportado.")

    return variant, label


def _supplier_cost_snapshot(product):
    """Congela o landed cost canônico do fornecedor sem inferir ausências.

    O snapshot só é completo quando existe exatamente um vínculo ativo, custo
    e frete válidos, moedas coerentes e landed_cost igual a custo + frete.
    Qualquer lacuna permanece NULL para a economia unitária falhar fechada.
    """
    empty = {
        "supplier_unit_cost_snapshot": None,
        "supplier_shipping_cost_snapshot": None,
        "supplier_landed_cost_snapshot": None,
        "supplier_cost_currency_snapshot": None,
    }

    active_links = [
        link
        for link in (getattr(product, "supplier_links", None) or [])
        if getattr(link, "is_active", False)
    ]
    if len(active_links) != 1:
        return empty

    link = active_links[0]
    try:
        unit_cost = money(link.cost)
        shipping_cost = money(link.shipping_cost)
        landed_cost = money(link.landed_cost)
    except (TypeError, ValueError):
        return empty

    currency = str(getattr(link, "currency", "") or "").strip().upper()
    freight_currency = str(
        getattr(link, "freight_currency", "") or ""
    ).strip().upper()

    if (
        unit_cost <= 0
        or shipping_cost < 0
        or landed_cost <= 0
        or landed_cost != money(unit_cost + shipping_cost)
        or not currency
        or currency != freight_currency
    ):
        return empty

    return {
        "supplier_unit_cost_snapshot": unit_cost,
        "supplier_shipping_cost_snapshot": shipping_cost,
        "supplier_landed_cost_snapshot": landed_cost,
        "supplier_cost_currency_snapshot": currency[:3],
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
        .filter(
            Product.id == product_id,
            Product.is_active.is_(True),
            Product.is_draft.is_(False),
        )
        .with_for_update()
        .first()
    )

    if not product:
        raise ValueError("Produto não encontrado ou indisponível.")

    quantity = _parse_quantity(
        request.form.get("quantity")
    )

    variant, selected_size = _resolve_payment_variant(
        product,
        request.form.get("variant_id"),
        request.form.get("selected_size")
        or request.form.get("size")
        or "Único",
    )

    if variant is not None:
        stock = getattr(variant, "stock", None)
    else:
        stock = getattr(product, "stock", None)

    if stock is not None and stock < quantity:
        raise ValueError(
            f"Estoque insuficiente para {product.name}."
        )

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
            "variant_id": (
                variant.id if variant is not None else None
            ),
            **_supplier_cost_snapshot(product),
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

        if not product or not product.is_active or product.is_draft:
            raise ValueError(
                "Um produto do carrinho não está mais disponível."
            )

        quantity = _parse_quantity(cart_item.quantity)

        variant, selected_size = _resolve_payment_variant(
            product,
            getattr(cart_item, "variant_id", None),
            cart_item.selected_size or "Único",
        )

        if variant is not None:
            stock = getattr(variant, "stock", None)
        else:
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
                "selected_size": selected_size,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": money(
                    unit_price * quantity
                ),
                "variant_id": (
                    variant.id if variant is not None else None
                ),
                **_supplier_cost_snapshot(product),
            }
        )

    return items, cart


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

    if coupon.first_purchase_only:
        previous_order = (
            Order.query
            .filter(
                Order.user_id == current_user.id,
                Order.status.in_(["paid", "manual_review"]),
            )
            .first()
        )
        if previous_order:
            raise ValueError(
                "Este cupom é exclusivo para a primeira compra."
            )

    discount = money(
        coupon.calculate_discount(subtotal)
    )

    return coupon, discount


def _reserve_stock(item_data):
    """Abate o estoque no momento da criação do pedido (reserva).

    Roda dentro da mesma transação do pedido: se algo falhar depois
    (ex.: erro no gateway), o rollback devolve o estoque automaticamente.
    """
    for item in item_data:
        product = (
            Product.query
            .filter_by(id=item["product_id"])
            .with_for_update()
            .first()
        )

        if product is None or not product.is_active or product.is_draft:
            raise ValueError(
                f"Produto {item['product_name']} não está mais disponível."
            )

        # Defesa em profundidade: produto que usa variantes (ex.: AliExpress)
        # NUNCA pode ser debitado apenas no agregado Product.stock. Sem
        # variant_id, bloqueia — jamais cai no fluxo legacy por selected_size.
        if _product_uses_variants(product) and not item.get("variant_id"):
            raise ValueError(
                f"A variação de {product.name} não foi informada."
            )

        variant = None

        if item.get("variant_id"):
            variant = (
                ProductVariant.query
                .filter_by(
                    id=item["variant_id"],
                    product_id=product.id,
                )
                .with_for_update()
                .first()
            )

            if variant is None or not variant.is_active:
                raise ValueError(
                    f"Uma variação de {product.name} não está mais disponível."
                )

        if variant is not None:
            if int(variant.stock or 0) < item["quantity"]:
                raise ValueError(
                    f"Estoque insuficiente para {product.name}."
                )
            variant.stock = int(variant.stock or 0) - item["quantity"]
            if product.stock is not None:
                product.stock -= item["quantity"]
        else:
            if product.stock is not None and product.stock < item["quantity"]:
                raise ValueError(
                    f"Estoque insuficiente para {product.name}."
                )
            if product.stock is not None:
                product.stock -= item["quantity"]


def _release_stock(order):
    """Devolve ao estoque o que foi reservado em um pedido não pago.

    Para itens com variante, resolve sempre Product primeiro e ProductVariant
    depois (ordem de lock consistente com _reserve_stock). Se a variante não
    resolver (id inexistente ou pertencente a outro produto), ABORTA com erro
    para que a transação faça rollback — jamais restaura apenas o agregado
    (Product.stock) e deixa a soma das variantes divergente.
    """
    if not order.stock_reserved:
        return

    for item in order.items:
        if item.variant_id is not None:
            # Guard: sem product_id não há como validar a integridade da
            # variante — aborta em vez de restaurar cegamente.
            if item.product_id is None:
                raise ValueError(
                    f"Item inválido em {item.product_name}: "
                    "variant_id sem product_id não pode ser restituído."
                )

            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .first()
            )
            if product is None:
                raise ValueError(
                    f"Produto não encontrado para restituir: {item.product_name}."
                )

            variant = (
                ProductVariant.query
                .filter_by(id=item.variant_id, product_id=item.product_id)
                .with_for_update()
                .first()
            )
            if variant is None:
                # Erro de integridade: não restaurar Product.stock isoladamente.
                raise ValueError(
                    f"Variação inválida ao restituir: {item.product_name}."
                )

            if variant.stock is not None:
                variant.stock += item.quantity
            if product.stock is not None:
                product.stock += item.quantity
        else:
            if item.product_id is None:
                continue

            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .first()
            )
            if product is not None and product.stock is not None:
                product.stock += item.quantity

    # Só após TODOS os itens restaurados com sucesso a reserva é encerrada.
    order.stock_reserved = False


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

    idempotency_key = None

    try:
        checkout_type = str(
            request.form.get("checkout_type") or ""
        ).strip().lower()

        if checkout_type not in ("product", "cart"):
            raise ValueError(
                "Tipo de checkout inválido."
            )

        # Reenvio do mesmo form (duplo clique, F5, voltar do Mercado Pago):
        # reaproveita o pedido já criado antes de qualquer efeito colateral.
        idempotency_key = _normalize_idempotency_key(
            request.form.get("idempotency_key")
        )
        duplicate = _find_duplicate_checkout(idempotency_key)
        if duplicate is not None:
            current_app.logger.info(
                "Checkout idempotente: reenvio detectado no pre-check; "
                "reaproveitando pedido %s.",
                duplicate.id,
            )
            return _redirect_to_existing_order(duplicate)

        if checkout_type == "product":
            item_data, cart = _get_direct_product_items(), None

        else:
            item_data, cart = _get_cart_items()

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

        customer_data = _get_customer_data()
        from loja.services.acquisition_costs import freeze_checkout_acquisition
        try:
            freeze_checkout_acquisition(item_data, customer_data["zip_code"])
        except QuoteUnavailable as exc:
            raise ValueError(_unavailable_message(exc.code)) from exc
        shipping = quote_checkout_shipping(item_data, customer_data["zip_code"])
        # Flag ligada exige confirmação agrupada por sessão; desligada usa
        # regra temporária flat aprovada, sem confirmação de remessas.
        if _is_grouped_quote_enabled():
            _require_confirmed_quote(item_data, customer_data["zip_code"], shipping)

        total = money(
            max(
                subtotal + shipping - discount,
                Decimal("0.00"),
            )
        )

        # Reserva o estoque na criação do pedido, impedindo que o mesmo
        # item seja vendido para outro cliente enquanto o pagamento pende.
        _reserve_stock(item_data)

        created_at = utcnow()
        order = Order(
            id=uuid.uuid4(),
            user_id=current_user.id,
            coupon_id=coupon.id if coupon else None,
            coupon_code=coupon.code if coupon else None,
            checkout_type=checkout_type,
            idempotency_key=idempotency_key,
            status="pending_payment",
            subtotal=subtotal,
            shipping=shipping,
            discount=discount,
            total=total,
            stock_reserved=True,
            created_at=created_at,
            mp_preference_expires_at=reservation_deadline(created_at),
            **customer_data,
            **acquisition_to_order_kwargs(
                order_acquisition(session=session)
            ),
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

        # O carrinho virou um pedido persistido. Em caso de pagamento pendente
        # ou falho, o pedido continua disponível no banco para acompanhamento,
        # mas os itens não devem permanecer disponíveis para uma nova compra.
        if cart is not None:
            for cart_item in list(cart.items):
                db.session.delete(cart_item)

        db.session.commit()

        session.pop("checkout_cart", None)
        session.pop("checkout_coupon", None)
        session.pop("checkout_product", None)
        session.modified = True

        return redirect(
            preference["checkout_url"]
        )

    except IntegrityError:
        # Reenvio concorrente do mesmo form: a constraint UNIQUE
        # (user_id, idempotency_key) venceu. O rollback desfaz a reserva
        # de estoque deste request; o pedido do vencedor já está gravado.
        db.session.rollback()

        duplicate = _find_duplicate_checkout(idempotency_key)
        if duplicate is not None:
            current_app.logger.info(
                "Checkout idempotente: constraint UNIQUE recusou o 2o "
                "INSERT; reaproveitando pedido %s.",
                duplicate.id,
            )
            return _redirect_to_existing_order(duplicate)

        current_app.logger.exception(
            "Erro de integridade ao criar pedido."
        )

        flash(
            "Ocorreu um erro ao processar seu pedido.",
            "danger",
        )

    except ValueError as error:
        db.session.rollback()

        # Um reenvio pode bater em "carrinho vazio"/"estoque insuficiente"
        # depois que o primeiro request concluiu; nesse caso reaproveita
        # o pedido em vez de mostrar erro.
        duplicate = _find_duplicate_checkout(idempotency_key)
        if duplicate is not None:
            current_app.logger.info(
                "Checkout idempotente: reenvio com erro de validacao; "
                "reaproveitando pedido %s.",
                duplicate.id,
            )
            return _redirect_to_existing_order(duplicate)

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

    referrer = request.referrer or ""
    if _safe_referrer(referrer):
        return redirect(referrer)

    return redirect(url_for("main.home"))


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
        .with_for_update(of=Order)
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
            order.paid_at = utcnow()
        db.session.commit()
        return order

    mapped_status = mp_status_to_order_status(mp_status)

    if mapped_status == "paid":
        if order.status in {"refunded", "charged_back"}:
            # Fase 8.0.1: approved tardio NUNCA reverte estorno — a ordem
            # permanece refunded/charged_back e o fulfillment continua como
            # cancelado/em cancelamento. O payment record acima já reflete o
            # payload; não há mudança de estado do pedido.
            current_app.logger.info(
                "Pagamento %s approved tardio ignorado: pedido %s já %s.",
                payment_id, order.id, order.status,
            )
        elif order.status == "paid":
            # Webhook approved repetido: re-ensure idempotente (Fase 8.0.1).
            # Mantém mesmas AliExpressOrder/idempotency_key/items.
            ensure_aliexpress_fulfillment(order)
        elif order.status not in {"manual_review"}:
            locked_products = {}
            locked_variants = {}
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
                    not order.stock_reserved
                    and product.stock is not None
                    and product.stock < item.quantity
                ):
                    review_reasons.append(
                        f"Estoque insuficiente: {item.product_name}."
                    )

                if (
                    product is not None
                    and _product_uses_variants(product)
                    and item.variant_id is None
                ):
                    # Integridade: produto que usa variantes (ex.: AliExpress)
                    # com OrderItem sem variant_id. NUNCA debitamos Product.stock
                    # isoladamente aqui — o pagamento vai para revisão manual com
                    # mensagem sanitizada (sem supplier_sku_id/external_sku_id).
                    review_reasons.append(
                        f"Variação não informada: {item.product_name}."
                    )

                if item.variant_id is not None:
                    variant = (
                        ProductVariant.query
                        .filter_by(id=item.variant_id, product_id=item.product_id)
                        .with_for_update()
                        .first()
                    )
                    locked_variants[item.id] = variant

                    # Integridade Product<->Variant: a variante deve existir E
                    # pertencer ao produto do item — validada SEMPRE,
                    # independentemente de stock_reserved. Uma variante
                    # inexistente ou de outro produto é erro de integridade e
                    # nunca deve virar "paid" silenciosamente.
                    if variant is None:
                        review_reasons.append(
                            f"Variação inválida: {item.product_name}."
                        )
                    elif not order.stock_reserved and (
                        not variant.is_active
                        or int(variant.stock or 0) < item.quantity
                    ):
                        # Estoque/disponibilidade só importam quando um novo
                        # débito será necessário (reserva já abatida dispensa).
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

            order.paid_at = order.paid_at or utcnow()

            if review_reasons:
                order.status = "manual_review"
                order.manual_review_reason = " ".join(review_reasons)
            else:
                # Pedidos com reserva já tiveram o estoque abatido na criação.
                if not order.stock_reserved:
                    for item in order.items:
                        product = locked_products.get(item.id)
                        if product is not None and product.stock is not None:
                            product.stock -= item.quantity

                        if item.variant_id is not None:
                            variant = locked_variants.get(item.id)
                            if (
                                variant is not None
                                and variant.stock is not None
                            ):
                                variant.stock -= item.quantity

                    # Pagamento aprovado tardio: o estoque foi debitado agora e
                    # precisa poder ser restituído em um eventual refund.
                    order.stock_reserved = True

                if locked_coupon is not None:
                    locked_coupon.times_used += 1
                    order.coupon_counted_at = utcnow()

                order.status = "paid"
                order.manual_review_reason = None

                # Fulfillment AliExpress (Fase 8.0) — idempotente, mesma transação.
                # Só roda para pedidos com status "paid" (não manual_review).
                # Não enfileira quando Order.status em: manual_review, payment_failed, refunded, charged_back
                if order.status == "paid":
                    ensure_aliexpress_fulfillment(order)

    elif mapped_status in {"refunded", "charged_back"}:
        _release_stock(order)
        order.status = mapped_status
        # O webhook continua sendo a única autoridade para confirmar o ledger
        # de refund iniciado após cancelamento do fornecedor (FASE 8.3.0-H).
        if mapped_status == "refunded" and order.supplier_refund_state is not None:
            order.supplier_refund_state = "confirmed"
            order.supplier_refund_confirmed_at = (
                order.supplier_refund_confirmed_at or utcnow()
            )
            order.supplier_refund_last_error = None
        # Cancelar fulfillment pré-submissão (Fase 8.0)
        cancel_fulfillment_before_submission(order)
    elif mapped_status == "payment_failed" and order.status not in {
        "paid",
        "manual_review",
        "refunded",
        "charged_back",
    }:
        _release_stock(order)
        order.status = mapped_status
    elif order.status not in {"paid", "manual_review", "refunded", "charged_back"}:
        order.status = mapped_status

    db.session.commit()
    return order


@payments_blueprint.route("/pagamentos/webhook", methods=["POST"])
@csrf.exempt
def mercado_pago_webhook():
    body = request.get_json(silent=True) or {}
    query_data_id = request.args.get("data.id")

    kind = classify_webhook(request.args, body)

    if kind == WebhookKind.LEGACY_IPN:
        # IPN legado (id + topic) não permite validar origem pela chave
        # secreta. Reconhecemos apenas para responder 200 e interromper
        # retries; NUNCA consultamos provider, sincronizamos ou gravamos.
        log_event(
            WH_LEGACY_IPN_IGNORED,
            level="info",
            legacy_topic=_clean_query_arg(request.args.get("topic")),
        )
        return jsonify({"ok": True}), 200

    if kind == WebhookKind.MALFORMED:
        # Payload desconhecido/malformado: sem processamento, sem provider,
        # sem banco. Resposta 200 apenas para não gerar retries.
        log_event(
            WH_MALFORMED_IGNORED,
            level="info",
            reason="malformed_or_unknown",
        )
        return jsonify({"ok": True}), 200

    try:
        validate_webhook_signature(
            request.headers.get("x-signature"),
            request.headers.get("x-request-id"),
            query_data_id,
        )
    except InvalidWebhookError:
        log_event(WH_MODERN_REJECTED, level="warning", reason="invalid_signature")
        current_app.logger.warning(
            "Webhook Mercado Pago com assinatura inválida; evento não processado."
        )
        return jsonify({"ok": False}), 401
    except PaymentConfigurationError:
        log_event(WH_MODERN_REJECTED, level="error", reason="configuration_error")
        current_app.logger.exception(
            "Webhook Mercado Pago sem configuração segura."
        )
        return jsonify({"ok": False}), 503

    log_event(WH_MODERN_VALIDATED, level="info")

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
        # O evento recebido é sincronizado antes de qualquer manutenção ampla.
        # A limpeza de reservas roda apenas por operação dedicada e protegida.
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


def _clean_query_arg(value):
    """Valor de query para telemetria legada, limitado e sem payload."""
    normalized = str(value or "").strip()
    if len(normalized) > 64:
        return normalized[:64]
    return normalized


def _valid_payment_id(value):
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.lower() in {"null", "none", "undefined"}:
        return None
    return normalized


@payments_blueprint.route("/pagamentos/retorno/<result>")
@login_required
def payment_return(result):
    payment_id = (
        _valid_payment_id(request.args.get("payment_id"))
        or _valid_payment_id(request.args.get("collection_id"))
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
