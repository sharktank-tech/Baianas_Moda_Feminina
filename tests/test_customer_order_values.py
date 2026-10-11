"""Valores do detalhe do pedido no /encomendas (espelha o sintético do QA).

Pedido pago: item 2×89,90, subtotal 179,80, frete 10,00, desconto 0,00,
total 189,80. O template lia `shipping_amount`/`discount_amount`/
`total_amount` (inexistentes → default 0): exibia frete "Grátis" e total
zerado. Agora usa os campos persistidos.
"""
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal


def _paid_order(db, user, product):
    from loja.modules.getway_mercado_pago.payment_models import (
        Order, OrderItem, Payment,
    )

    order = Order(
        id=uuid.uuid4(), user_id=user.id, checkout_type="product",
        status="paid", subtotal=Decimal("179.80"), shipping=Decimal("10.00"),
        discount=Decimal("0.00"), total=Decimal("189.80"),
        customer_name="Cliente Valores", customer_email=user.email,
        zip_code="77066520", street="Rua Teste", number="1",
        neighborhood="Centro", city="Palmas", state="TO",
        mp_payment_id=uuid.uuid4().hex, mp_status="approved",
        paid_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        id=uuid.uuid4(), order_id=order.id, product_id=product.id,
        product_name=product.name, quantity=2,
        unit_price=Decimal("89.90"), line_total=Decimal("179.80"),
    )
    order.payments.append(Payment(
        id=uuid.uuid4(), provider="mercado_pago",
        provider_payment_id=order.mp_payment_id, status="approved",
        amount=order.total, currency="BRL", raw_payload={},
    ))
    db.session.add(item)
    db.session.commit()
    return order


def test_detalhe_exibe_valores_persistidos(client, db, make_user, make_product,
                                           login):
    owner = make_user()
    product = make_product(stock=10)
    order = _paid_order(db, owner, product)
    login(owner.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)

    assert "179,80" in html  # itens + subtotal
    assert "10,00" in html  # frete cobrado
    assert "Grátis" not in html  # frete > 0 nunca é gratuito
    assert "Pago" in html  # estado em português

    total = re.search(r"Total\s*</span>\s*<strong>\s*R\$\s*([\d.,]+)",
                      html)
    assert total is not None and total.group(1) == "189,80"
    discount = re.search(r"Desconto\s*</span>\s*<strong>\s*− R\$\s*([\d.,]+)",
                         html)
    assert discount is not None and discount.group(1) == "0,00"
