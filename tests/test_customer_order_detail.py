"""Pedido sintético: admin, proprietário e rastreio, sem rede/produção."""
from decimal import Decimal
from html.parser import HTMLParser
import uuid

import pytest
import requests

from loja.modules.getway_mercado_pago.payment_models import OrderItem
from loja.modules.models import Shipment
from test_admin_orders_and_payment_confirmation import make_order, add_evidence


class _ClassText(HTMLParser):
    def __init__(self, css_class):
        super().__init__()
        self.css_class = css_class
        self.depth = 0
        self.active = []
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag in {"input", "img", "br", "hr", "meta", "link", "source"}:
            return
        self.depth += 1
        if self.css_class in dict(attrs).get("class", "").split():
            self.active.append((self.depth, []))

    def handle_endtag(self, tag):
        if tag in {"input", "img", "br", "hr", "meta", "link", "source"}:
            return
        for depth, text in list(self.active):
            if depth == self.depth:
                self.results.append(" ".join("".join(text).split()))
                self.active.remove((depth, text))
        self.depth -= 1

    def handle_data(self, data):
        for _, text in self.active:
            text.append(data)


def class_text(html, css_class):
    parser = _ClassText(css_class)
    parser.feed(html)
    assert len(parser.results) == 1, (css_class, parser.results)
    return parser.results[0]


def order_with_item(db, user, status="paid"):
    order = make_order(db, user, status)
    order.items.append(OrderItem(
        id=uuid.uuid4(), product_name="Vestido sintético",
        selected_size="M", quantity=1, unit_price=Decimal("58"),
        line_total=Decimal("58"),
    ))
    db.session.commit()
    return order


def forbid_side_effects(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Consulta não pode gravar nem chamar serviços externos")
    monkeypatch.setattr(db.session, "commit", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def test_customer_detail_uses_checkout_snapshot(db, client, login, make_user, monkeypatch):
    user = make_user()
    order = order_with_item(db, user)
    add_evidence(db, order)
    login(user.email)
    forbid_side_effects(db, monkeypatch)
    response = client.get(f"/encomendas/pedido/{order.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert class_text(html, "order-total") == "Total R$ 68,00"
    values = class_text(html, "order-values")
    assert "Frete R$ 10,00" in values
    assert "Grátis" not in values
    assert "M" in class_text(html, "order-product-options").split()
    assert "CEP 77066520" in html
    assert "Pagamento confirmado" in class_text(html, "order-payment-status")


def test_customer_detail_discount_and_quantity_are_snapshots(db, client, login, make_user):
    user = make_user()
    order = order_with_item(db, user)
    order.subtotal = Decimal("116")
    order.discount = Decimal("5")
    order.total = Decimal("121")
    order.items[0].quantity = 2
    order.items[0].line_total = Decimal("116")
    db.session.commit()
    login(user.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert class_text(html, "order-total") == "Total R$ 121,00"
    assert "5,00" in class_text(html, "order-discount")
    assert "116,00" in class_text(html, "order-product-price")
    assert "Pagamento não confirmado" in class_text(html, "order-payment-status")


def test_customer_detail_free_shipping_only_when_snapshot_zero(db, client, login, make_user):
    user = make_user()
    order = order_with_item(db, user)
    order.shipping = Decimal("0")
    order.total = Decimal("58")
    db.session.commit()
    login(user.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert class_text(html, "order-free") == "Grátis"
    assert class_text(html, "order-total") == "Total R$ 58,00"


def test_provider_status_without_payment_record_is_not_confirmation(db, client, login, make_user):
    from loja.utils import utcnow
    user = make_user()
    order = order_with_item(db, user)
    order.mp_status = "approved"
    order.mp_payment_id = "SYNTHETIC-PAYMENT"
    order.paid_at = utcnow()
    db.session.commit()
    login(user.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert "Pagamento não confirmado" in class_text(html, "order-payment-status")


@pytest.mark.parametrize("status,heading", [
    ("pending_payment", "Estamos aguardando a confirmação do pagamento."),
    ("paid", "Seu pedido está sendo preparado."),
    ("expired", "Pedido expirado"),
    ("cancelled", "Este pedido foi cancelado."),
])
def test_customer_detail_status_matches_persisted_order(db, client, login, make_user, status, heading):
    user = make_user()
    order = order_with_item(db, user, status)
    login(user.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert heading in class_text(html, "order-status-content")
    if status == "pending_payment":
        assert "Pagamento confirmado" not in html


@pytest.mark.parametrize("shipment_status,label,heading", [
    ("aguardando_postagem", "Aguardando envio", "Seu pedido está sendo preparado."),
    ("em_transito", "Enviado", "Sua encomenda já está a caminho."),
    ("entregue", "Aguardando avaliação", "Sua encomenda foi entregue."),
])
def test_customer_list_detail_and_admin_share_shipment(db, client, login, make_user, shipment_status, label, heading):
    user = make_user(is_admin=True)
    order = order_with_item(db, user)
    code = "SYNTHETIC" + uuid.uuid4().hex[:10].upper()
    shipment = Shipment(id=uuid.uuid4().int % (2**62), order_id=order.id,
                        tracking_code=code, carrier="Transportadora sintética",
                        status=shipment_status, provider="manual")
    db.session.add(shipment)
    db.session.commit()
    login(user.email)
    listing = client.get("/encomendas").get_data(as_text=True)
    assert label in listing and code in listing
    detail = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert code in detail
    assert heading in class_text(detail, "order-status-content")
    admin = client.get(f"/admin/pedidos/{order.id}").get_data(as_text=True)
    assert code in admin and "Transportadora sintética" in admin


def test_owner_cannot_view_other_customers_order_or_admin(db, client, login, make_user):
    owner, other = make_user(), make_user()
    own_order = order_with_item(db, owner)
    foreign = order_with_item(db, other)
    login(owner.email)
    listing = client.get("/encomendas").get_data(as_text=True)
    assert own_order.order_number in listing
    assert foreign.order_number not in listing
    assert client.get(f"/encomendas/pedido/{foreign.id}").status_code == 404
    assert client.get(f"/admin/pedidos/{own_order.id}").status_code == 302


def test_anonymous_customer_orders_require_login(client):
    for path in ["/conta", "/encomendas", f"/encomendas/pedido/{uuid.uuid4()}"]:
        response = client.get(path)
        assert response.status_code == 302 and "/login" in response.headers["Location"]
