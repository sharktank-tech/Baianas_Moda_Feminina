"""FASE 9.0.0-B — métricas por canal (somente leitura).

Cobre o serviço agregador e a rota GET /admin/metricas-por-canal.
Nenhum provider é consultado; pedidos com origem vazia ficam em "direto".
"""
import uuid
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.services.channel_metrics import build_channel_metrics
from loja.services.unit_economics_policy import UnitEconomicsCostPolicy


def _complete_policy():
    return UnitEconomicsCostPolicy(
        gateway_fee_percent=Decimal("3.00"),
        installment_cost_percent=Decimal("1.00"),
        tax_percent=Decimal("2.00"),
        acquisition_cost_per_order=Decimal("1.00"),
        refund_reserve_percent=Decimal("1.00"),
    )


def _order(
    *,
    status="paid",
    total="68.80",
    source=None,
    medium=None,
    referrer=None,
    email="cliente@example.com",
    user_id=None,
):
    return Order(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        checkout_type="product",
        status=status,
        subtotal=Decimal("49.90"),
        shipping=Decimal("18.90"),
        discount=Decimal("0.00"),
        total=Decimal(total),
        stock_reserved=True,
        customer_name="Cliente",
        customer_email=email,
        zip_code="01310100",
        street="Rua",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        acquisition_source=source,
        acquisition_medium=medium,
        acquisition_referrer=referrer,
    )


def _order_with_cost_snapshot(user_id, product, *, total="68.80", unit="40.00",
                              shipping="10.00", source="google"):
    order = _order(total=total, source=source, user_id=user_id)
    order.items.append(
        OrderItem(
            id=uuid.uuid4(),
            product_id=product.id,
            product_name=product.name,
            selected_size="M",
            quantity=1,
            unit_price=Decimal("80.00"),
            line_total=Decimal("80.00"),
            supplier_unit_cost_snapshot=Decimal(unit),
            supplier_shipping_cost_snapshot=Decimal(shipping),
            supplier_landed_cost_snapshot=Decimal(unit) + Decimal(shipping),
            supplier_cost_currency_snapshot="BRL",
        )
    )
    return order


def test_build_channel_metrics_groups_by_source(db):
    orders = [
        _order(source="instagram"),
        _order(source="instagram"),
        _order(source="google"),
        _order(source=None, referrer=None),
    ]
    rows, totals = build_channel_metrics(orders)

    by_channel = {row.channel: row for row in rows}
    assert by_channel["instagram"].orders == 2
    assert by_channel["google"].orders == 1
    assert by_channel["direto"].orders == 1
    assert totals.orders == 4


def test_conversion_ticket_and_repeat(db):
    orders = [
        _order(status="paid", total="100.00", source="facebook",
               email="a@example.com"),
        _order(status="paid", total="50.00", source="facebook",
               email="a@example.com"),
        _order(status="pending_payment", source="facebook"),
    ]
    rows, totals = build_channel_metrics(orders)
    row = next(r for r in rows if r.channel == "facebook")

    assert row.orders == 3
    assert row.paid_orders == 2
    # conversão = 2/3 ≈ 0.666666… quantizado para 0.67
    assert row.conversion == Decimal("0.67")
    assert row.revenue == Decimal("150.00")
    assert row.avg_ticket == Decimal("75.00")
    assert row.repeat_customers == 1


def test_revenue_none_when_no_paid(db):
    orders = [_order(status="pending_payment", total="10.00", source="ig")]
    rows, totals = build_channel_metrics(orders)
    row = rows[0]

    assert row.revenue is None
    assert row.avg_ticket is None
    assert totals.revenue is None


def test_margin_uses_cost_snapshot_and_fail_closed(db, make_product):
    product = make_product(stock=5)
    user_id = uuid.uuid4()
    orders = [
        _order_with_cost_snapshot(user_id, product),  # margem conhecida
        _order_with_cost_snapshot(user_id, product),
        _order(user_id=user_id, source="google"),  # sem snapshot items -> desconhecida
    ]
    rows, totals = build_channel_metrics(orders, policy=_complete_policy())
    row = rows[0]

    assert row.margin_known == 2
    assert row.avg_margin is not None
    assert row.avg_margin > Decimal("0.00")
    assert totals.margin_known_orders == 2
    assert totals.margin_known_value is not None


def test_margin_incomplete_kept_out_of_avg(db, make_product):
    product = make_product(stock=5)
    order = _order_with_cost_snapshot(uuid.uuid4(), product)
    order.items = []  # itens removidos → snapshot ausente
    rows, totals = build_channel_metrics([order], policy=_complete_policy())

    assert rows[0].margin_known == 0
    assert rows[0].avg_margin is None
    assert totals.margin_known_orders == 0


def test_grouping_prefers_source_over_referrer(db):
    orders = [_order(source="instagram", referrer="tiktok.com")]
    rows, totals = build_channel_metrics(orders)
    assert rows[0].channel == "instagram"


def test_referrer_fallback_channel(db):
    orders = [_order(source=None, referrer="tiktok.com")]
    rows, totals = build_channel_metrics(orders)
    assert rows[0].channel == "referrer:tiktok.com"


# ---------------------------------------------------------------------------
# Rota admin (somente leitura)
# ---------------------------------------------------------------------------

def test_metrics_route_admin(client, admin_user, login, db, make_user):
    buyer = make_user()
    db.session.add(_order(source="instagram", user_id=buyer.id))
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.get("/admin/metricas-por-canal")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Métricas por Canal" in body
    assert "instagram" in body
    assert "direto" in body


def test_metrics_route_anonymous_redirect(client):
    response = client.get("/admin/metricas-por-canal")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_metrics_route_non_admin_denied(client, regular_user, login):
    login(regular_user.email, "senha123")
    response = client.get("/admin/metricas-por-canal", follow_redirects=True)
    assert response.status_code == 200
    assert "Métricas por Canal" not in response.get_data(as_text=True)