"""Customer confirmations require payment evidence; admin lookup is read-only."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order, Payment
from loja.services.order_payment_evidence import payment_is_confirmed
import loja.views.payments_routes as routes


def make_order(db, user, status="pending_payment"):
    order = Order(
        id=uuid.uuid4(), user_id=user.id, checkout_type="product", status=status,
        subtotal=Decimal("58"), shipping=Decimal("10"), discount=Decimal("0"),
        total=Decimal("68"), customer_name="Cliente de teste", customer_email=user.email,
        zip_code="77066520", street="Rua Teste", number="1",
        neighborhood="Centro", city="Palmas", state="TO",
    )
    db.session.add(order)
    db.session.commit()
    return order


def add_evidence(db, order, amount="68", currency="BRL", status="approved"):
    order.mp_payment_id = uuid.uuid4().hex
    order.mp_status = "approved"
    order.paid_at = datetime.now(timezone.utc)
    order.payments.append(Payment(
        id=uuid.uuid4(), provider="mercado_pago",
        provider_payment_id=order.mp_payment_id, status=status,
        amount=Decimal(amount), currency=currency,
        raw_payload={"secret": "RAW_PAYLOAD_MUST_NOT_APPEAR"},
    ))
    db.session.commit()


@pytest.mark.parametrize("status", ["pending_payment", "manual_review", "paid"])
def test_success_url_is_not_payment_proof(db, client, login, make_user, status):
    user = make_user()
    order = make_order(db, user, status)
    login(user.email)
    html = client.get("/pagamentos/retorno/sucesso", query_string={
        "order_id": str(order.id), "status": "approved",
    }).get_data(as_text=True)
    assert "<h1>Pagamento aprovado</h1>" not in html
    assert "<h1>Pagamento recebido</h1>" not in html
    assert order.order_number in html
    assert "Acompanhar pedido" in html


@pytest.mark.parametrize("status,heading", [("paid", "Pagamento aprovado"),
                                           ("manual_review", "Pagamento recebido")])
def test_confirmed_payment_shows_correct_heading(db, client, login, make_user, status, heading):
    user = make_user()
    order = make_order(db, user, status)
    add_evidence(db, order)
    login(user.email)
    html = client.get("/pagamentos/retorno/sucesso", query_string={
        "order_id": str(order.id),
    }).get_data(as_text=True)
    assert f"<h1>{heading}</h1>" in html


@pytest.mark.parametrize("amount,currency,status", [
    ("67", "BRL", "approved"), ("68", "USD", "approved"),
    ("68", "BRL", "pending"),
])
def test_invalid_payment_evidence_does_not_confirm(db, make_user, amount, currency, status):
    order = make_order(db, make_user(), "paid")
    add_evidence(db, order, amount, currency, status)
    assert not payment_is_confirmed(order)


def test_return_cannot_sync_another_customers_payment(db, client, login, make_user, monkeypatch):
    owner, visitor = make_user(), make_user()
    order = make_order(db, owner)
    counts = (Order.query.count(), Payment.query.count())
    monkeypatch.setattr(routes, "get_payment", lambda _: {
        "id": "foreign-payment", "external_reference": str(order.id),
        "status": "approved", "transaction_amount": 68, "currency_id": "BRL",
    })
    login(visitor.email)
    response = client.get("/pagamentos/retorno/sucesso", query_string={
        "payment_id": "foreign-payment", "order_id": str(order.id),
    })
    assert response.status_code == 200
    assert order.order_number not in response.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Order, order.id).status == "pending_payment"
    assert (Order.query.count(), Payment.query.count()) == counts


def test_missing_order_never_shows_approval(client, login, make_user):
    login(make_user().email)
    html = client.get("/pagamentos/retorno/sucesso", query_string={
        "order_id": str(uuid.uuid4()), "status": "approved",
    }).get_data(as_text=True)
    assert "<h1>Pagamento aprovado</h1>" not in html
    assert "antes de pagar novamente" in html


def test_return_resolves_owned_order_from_provider_without_query_reference(
    db, client, login, make_user, monkeypatch,
):
    user = make_user()
    order = make_order(db, user)
    login(user.email)
    calls = []
    def sync(payment_id, *, expected_user_id):
        calls.append((payment_id, expected_user_id))
        return order
    monkeypatch.setattr(routes, "_sync_payment", sync)
    html = client.get("/pagamentos/retorno/sucesso?payment_id=123").get_data(as_text=True)
    assert calls == [("123", user.id)]
    assert order.order_number in html
    assert "<h1>Pagamento pendente</h1>" in html


@pytest.mark.parametrize("missing", ["paid_at", "mp_status", "mp_payment_id"])
def test_incomplete_order_evidence_does_not_confirm(db, make_user, missing):
    order = make_order(db, make_user(), "paid")
    add_evidence(db, order)
    setattr(order, missing, None)
    assert not payment_is_confirmed(order)
    db.session.rollback()


@pytest.mark.parametrize("admin", [False, True])
def test_admin_orders_permissions(db, client, login, make_user, admin):
    user = make_user(is_admin=admin)
    order = make_order(db, user)
    login(user.email)
    for path in ["/admin/pedidos", f"/admin/pedidos/{order.id}"]:
        response = client.get(path)
        assert response.status_code == (200 if admin else 302)


def test_admin_orders_anonymous(client):
    assert client.get("/admin/pedidos").status_code == 302
    assert client.get(f"/admin/pedidos/{uuid.uuid4()}").status_code == 302


def test_admin_lookup_is_read_only_and_hides_raw_payment(db, client, login, admin_user, monkeypatch):
    order = make_order(db, admin_user, "paid")
    add_evidence(db, order)
    login(admin_user.email)
    def forbidden(*args, **kwargs):
        raise AssertionError("Consultas admin não devem gravar ou consultar o provedor")
    monkeypatch.setattr(db.session, "commit", forbidden)
    monkeypatch.setattr(routes, "get_payment", forbidden)
    for reference in [order.order_number, str(order.id), order.mp_payment_id]:
        response = client.get("/admin/pedidos", query_string={"q": reference})
        assert response.status_code == 200
        assert order.order_number in response.get_data(as_text=True)
    html = client.get(f"/admin/pedidos/{order.id}").get_data(as_text=True)
    assert "Confirmado" in html
    assert "RAW_PAYLOAD_MUST_NOT_APPEAR" not in html
    assert "68,00" in html
    assert "Nenhum rastreio registrado" in html
    assert client.post(f"/admin/pedidos/{order.id}").status_code == 405
    assert client.get(f"/admin/pedidos/{uuid.uuid4()}").status_code == 404
    assert client.get("/admin/pedidos?status=invalid").status_code == 400


def test_admin_status_filter_and_dashboard_link(db, client, login, admin_user):
    pending = make_order(db, admin_user)
    paid = make_order(db, admin_user, "paid")
    login(admin_user.email)
    html = client.get("/admin/pedidos?status=pending_payment").get_data(as_text=True)
    assert pending.order_number in html
    assert paid.order_number not in html
    html = client.get("/admin").get_data(as_text=True)
    assert html.count('href="/admin/pedidos"') >= 2


def test_admin_list_paginates_without_duplicate_orders(db, client, login, admin_user):
    orders = [make_order(db, admin_user, "charged_back") for _ in range(26)]
    login(admin_user.email)
    first = client.get("/admin/pedidos?status=charged_back").get_data(as_text=True)
    second = client.get("/admin/pedidos?status=charged_back&page=2").get_data(as_text=True)
    assert sum(order.order_number in first for order in orders) == 25
    assert sum(order.order_number in second for order in orders) == 1
    assert all(not (order.order_number in first and order.order_number in second) for order in orders)
    assert "Próxima" in first
