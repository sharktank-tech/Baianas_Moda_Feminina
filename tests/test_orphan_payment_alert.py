"""Item 1.6: alerta sanitizado de pagamento órfão no webhook.

Webhook que recebe um pagamento sem pedido correspondente — referência
``external_reference`` não-UUID ou UUID sem linha em ``orders`` — emite o
evento fixo ``orphan_payment_ignored`` via ``log_event`` (padrão issue #50):
identificadores hasheados por ``stable_hash`` e correlation id, nunca
identificador cru no texto do log.
"""

import json
import uuid
from datetime import timedelta
from decimal import Decimal

import loja.views.payments_routes as pr
from loja.observability.mercado_pago import (
    WH_ORPHAN_PAYMENT_IGNORED,
    stable_hash,
)
from loja.utils import utcnow


def _mp_payload(payment_id, external_reference, *, status="approved", amount="30.00"):
    return {
        "id": payment_id,
        "external_reference": external_reference,
        "transaction_amount": amount,
        "currency_id": "BRL",
        "status": status,
        "status_detail": "accredited",
    }


def _make_order(db, user, product, *, quantity=2):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    unit_price = Decimal(str(product.price))
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=unit_price * quantity,
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=unit_price * quantity + Decimal("10.00"),
        stock_reserved=True,
        created_at=utcnow() - timedelta(hours=2),
        mp_preference_expires_at=utcnow() + timedelta(hours=22),
        customer_name="Cliente Teste",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    order.items.append(
        OrderItem(
            id=uuid.uuid4(),
            product_id=product.id,
            product_name=product.name,
            selected_size="M",
            quantity=quantity,
            unit_price=unit_price,
            line_total=unit_price * quantity,
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


def _orphan_events(caplog):
    payloads = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("mp_observability "):
            continue
        payload = json.loads(message[len("mp_observability "):])
        if payload.get("event") == WH_ORPHAN_PAYMENT_IGNORED:
            payloads.append(payload)
    return payloads


def test_item_1_6_orphan_order_emits_sanitized_event(db, monkeypatch, caplog):
    caplog.set_level("WARNING")
    external_reference = str(uuid.uuid4())
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, external_reference),
    )

    synced = pr._sync_payment("orphan-payment-1")

    assert synced is None
    events = _orphan_events(caplog)
    assert len(events) == 1
    payload = events[0]
    assert payload["reason"] == "order_not_found"
    assert payload["correlation_id"]

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert external_reference not in text
    assert payload["external_reference"] == stable_hash(external_reference)
    assert "orphan-payment-1" not in text
    assert payload["payment_id"] == stable_hash("orphan-payment-1")


def test_item_1_6_invalid_reference_emits_sanitized_event(db, monkeypatch, caplog):
    caplog.set_level("WARNING")
    garbage_reference = "not-a-uuid-garbage!"
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, garbage_reference),
    )

    synced = pr._sync_payment("orphan-payment-2")

    assert synced is None
    events = _orphan_events(caplog)
    assert len(events) == 1
    payload = events[0]
    assert payload["reason"] == "invalid_external_reference"

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert garbage_reference not in text
    assert payload["external_reference"] == stable_hash(garbage_reference)
    assert "orphan-payment-2" not in text
    assert payload["payment_id"] == stable_hash("orphan-payment-2")


def test_item_1_6_known_order_emits_no_orphan_event(
    db, make_user, make_product, monkeypatch, caplog
):
    caplog.set_level("WARNING")
    user = make_user()
    product = make_product(stock=10)
    order = _make_order(db, user, product)
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, str(order.id), amount=str(order.total)),
    )

    synced = pr._sync_payment("sync-known-1")

    assert synced is not None
    assert synced.status == "paid"
    assert _orphan_events(caplog) == []
