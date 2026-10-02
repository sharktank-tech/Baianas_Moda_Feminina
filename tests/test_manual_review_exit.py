"""Item 1.3 — rota de saída de manual_review (aprovar / rejeitar).

Cobre em SQLite:
- aprovação: revalidação de estoque/variante sob lock, promoção a paid,
  débito apenas quando não havia reserva, paid_at e auditoria na linha;
- rejeição: devolução da reserva pelo mesmo caminho do webhook, status
  cancelled, motivo obrigatório e auditoria na linha;
- fail-closed: estoque insuficiente/variante inválida/não-manual_review
  não alteram nada; decisão fora de {'approved','rejected'} viola CHECK;
- webhook (guards do _sync_payment): approved tardio e payload divergente
  NÃO reanimam um cancelled; divergente continua gerando manual_review em
  pending_payment (controle);
- admin: lista restrita, aprovação exige confirmação, rejeição exige
  motivo; página de resultado exibe o texto aprovado de cancelled.

A migração 0032 (colunas de auditoria) é verificada em PG em
tests/test_manual_review_decision_pg.py.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

import loja.views.payments_routes as pr
from loja.services import order_promotion


def _manual_order(
    db,
    user,
    product,
    *,
    quantity=1,
    reserved=False,
    reason="Valor/moeda divergente: recebido BRL 1,00; esperado BRL 30,00.",
    total="30.00",
):
    """Pedido travado em manual_review (o estado que a rota precisa sair)."""
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    if reserved:
        # Reserva consumiu o estoque na criação (sempre).
        product.stock = int(product.stock or 0) - quantity
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="manual_review",
        manual_review_reason=reason,
        subtotal=Decimal(total),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal(total),
        stock_reserved=reserved,
        customer_name="Cliente Revisão",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua Sintética",
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
            unit_price=Decimal(total),
            line_total=Decimal(total),
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


def _spy_ensure(monkeypatch):
    """Substitui o fulfillment por um registrador (rede nunca é tocada)."""
    calls = []
    monkeypatch.setattr(
        order_promotion,
        "ensure_aliexpress_fulfillment",
        lambda order: calls.append(order.id) or [],
    )
    return calls


def _reload(db, order):
    db.session.expire_all()
    from loja.modules.getway_mercado_pago.payment_models import Order

    return db.session.get(Order, order.id)


# ── Aprovação ────────────────────────────────────────────────────────────────

def test_item_1_3_approve_revalidates_and_promotes_paid(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, quantity=2, reserved=False)
    calls = _spy_ensure(monkeypatch)
    stock_before = product.stock

    result = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id, note="valor conferido"
    )

    assert result.ok and result.outcome == "changed"
    order = _reload(db, order)
    assert order.status == "paid"
    assert order.manual_review_reason is None
    assert order.stock_reserved is True
    assert order.paid_at is not None
    assert order.manual_review_decision == "approved"
    assert order.manual_review_decided_at is not None
    assert order.manual_review_decided_by_user_id == user.id
    assert order.manual_review_decided_reason == "valor conferido"
    assert product.stock == stock_before - 2  # débito sob lock
    assert calls == [order.id]  # fulfillment enfileirado na mesma transação


def test_item_1_3_approve_blocked_when_stock_insufficient(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=1)
    order = _manual_order(db, user, product, quantity=2, reserved=False)
    calls = _spy_ensure(monkeypatch)
    reason_before = order.manual_review_reason

    result = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id
    )

    assert not result.ok and result.outcome == "blocked"
    assert any("Estoque insuficiente" in r for r in result.reasons)
    order = _reload(db, order)
    assert order.status == "manual_review"
    assert order.manual_review_reason == reason_before
    assert order.stock_reserved is False
    assert order.manual_review_decision is None
    assert product.stock == 1  # nada debitado
    assert calls == []  # sem fulfillment para pedido bloqueado


def test_item_1_3_approve_blocked_missing_variant_integrity(
    db, make_user, make_product, monkeypatch
):
    """Reserva existente não dispensa a integridade da variante."""
    user = make_user()
    product = make_product(stock=5)
    order = _manual_order(db, user, product, quantity=1, reserved=True)
    order.items[0].variant_id = uuid.uuid4()  # variante inexistente
    db.session.commit()
    calls = _spy_ensure(monkeypatch)

    result = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id
    )

    assert result.outcome == "blocked"
    assert any("Variação inválida" in r for r in result.reasons)
    order = _reload(db, order)
    assert order.status == "manual_review"
    assert calls == []


def test_item_1_3_approve_keeps_existing_reservation_no_debit(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, quantity=2, reserved=True)
    stock_after_reserve = product.stock  # 8 — já abatido na criação
    _spy_ensure(monkeypatch)

    result = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id
    )

    assert result.ok
    order = _reload(db, order)
    assert order.status == "paid"
    assert order.stock_reserved is True
    assert product.stock == stock_after_reserve  # sem débito duplo


# ── Rejeição ────────────────────────────────────────────────────────────────

def test_item_1_3_reject_restitutes_stock_and_cancels(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, quantity=2, reserved=True)
    assert product.stock == 8

    result = order_promotion.reject_manual_review(
        order.id, admin_user_id=user.id, reason="valor não confere com o MP"
    )

    assert result.ok and result.outcome == "changed"
    order = _reload(db, order)
    assert order.status == "cancelled"
    assert order.stock_reserved is False
    assert product.stock == 10  # reserva devolvida
    assert order.manual_review_decision == "rejected"
    assert order.manual_review_decided_at is not None
    assert order.manual_review_decided_by_user_id == user.id
    assert order.manual_review_decided_reason == "valor não confere com o MP"


def test_item_1_3_reject_without_reservation_keeps_stock(
    db, make_user, make_product
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, quantity=2, reserved=False)

    result = order_promotion.reject_manual_review(
        order.id, admin_user_id=user.id, reason="suspeita de fraude"
    )

    assert result.ok
    order = _reload(db, order)
    assert order.status == "cancelled"
    assert order.stock_reserved is False
    assert product.stock == 10  # nada a devolver e nada a estragar


def test_item_1_3_reject_requires_reason(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)

    result = order_promotion.reject_manual_review(
        order.id, admin_user_id=user.id, reason="   "
    )

    assert not result.ok and result.outcome == "blocked"
    assert result.reasons == ("reason_required",)
    order = _reload(db, order)
    assert order.status == "manual_review"
    assert order.stock_reserved is True  # estoque intacto


# ── Fail-closed de transição ────────────────────────────────────────────────

def test_item_1_3_approve_reject_noop_when_not_manual_review(
    db, make_user, make_product
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)
    order.status = "paid"
    db.session.commit()

    approve = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id
    )
    reject = order_promotion.reject_manual_review(
        order.id, admin_user_id=user.id, reason="qualquer"
    )

    assert approve.outcome == "noop"
    assert reject.outcome == "noop"
    order = _reload(db, order)
    assert order.status == "paid"  # nenhuma das duas ações tocou
    assert order.manual_review_decision is None


def test_item_1_3_double_approve_keeps_first_audit(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)

    first = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id, note="primeira"
    )
    decided_at = _reload(db, order).manual_review_decided_at
    second = order_promotion.approve_manual_review(
        order.id, admin_user_id=user.id, note="segunda"
    )

    assert first.ok and second.outcome == "noop"
    order = _reload(db, order)
    assert order.manual_review_decided_reason == "primeira"
    assert order.manual_review_decided_at == decided_at


def test_item_1_3_decision_constraint_rejects_unknown_value(
    db, make_user, make_product
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product)

    order.manual_review_decision = "maybe"
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# ── Guards do webhook (_sync_payment) ───────────────────────────────────────

def _mp_payload(payment_id, order, status, detail, amount=None):
    return {
        "id": payment_id,
        "external_reference": str(order.id),
        "transaction_amount": str(amount if amount is not None else order.total),
        "currency_id": "BRL",
        "status": status,
        "status_detail": detail,
    }


def test_item_1_3_webhook_approved_does_not_revive_cancelled(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)
    order.status = "cancelled"
    db.session.commit()
    stock_before = product.stock

    monkeypatch.setattr(
        pr, "get_payment", lambda pid: _mp_payload(pid, order, "approved", "accredited")
    )
    synced = pr._sync_payment("9130")

    assert synced.status == "cancelled"  # approved tardio não reanima
    assert product.stock == stock_before


def test_item_1_3_webhook_divergent_does_not_leave_cancelled(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)
    order.status = "cancelled"
    db.session.commit()
    reason_before = order.manual_review_reason

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited", amount="999.99"),
    )
    synced = pr._sync_payment("9131")

    assert synced.status == "cancelled"  # não cai para manual_review de novo
    assert synced.manual_review_reason == reason_before
    assert synced.mp_status == "approved"  # payload fica no Payment/Order


def test_item_1_3_webhook_divergent_still_review_on_pending(
    db, make_user, make_product, monkeypatch
):
    """Controle: o guard novo não quebra a via normal divergente→review."""
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)
    order.status = "pending_payment"
    order.manual_review_reason = None
    db.session.commit()

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited", amount="999.99"),
    )
    synced = pr._sync_payment("9132")

    assert synced.status == "manual_review"
    assert "Valor/moeda divergente" in synced.manual_review_reason


# ── Rotas admin ─────────────────────────────────────────────────────────────

def test_item_1_3_route_list_requires_admin(
    client, db, regular_user, login, make_product
):
    user = regular_user
    product = make_product(stock=10)
    order = _manual_order(db, user, product)

    login(user.email, "senha123")
    response = client.get("/admin/pedidos/revisao")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    followed = client.get("/admin/pedidos/revisao", follow_redirects=True)
    assert "não possui permissão" in followed.get_data(as_text=True)


def test_item_1_3_route_approve_flow(
    client, db, admin_user, login, make_product, monkeypatch
):
    user = admin_user
    product = make_product(stock=10)
    order = _manual_order(db, user, product, quantity=3, reserved=False)
    _spy_ensure(monkeypatch)

    login(user.email, "senha123")
    response = client.post(
        f"/admin/pedidos/revisao/{order.id}/aprovar",
        data={"confirm_approve": "1", "note": "conferido"},
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert "aprovado: estoque revalidado" in html
    order = _reload(db, order)
    assert order.status == "paid"
    assert order.manual_review_decision == "approved"
    assert order.stock_reserved is True
    assert product.stock == 7


def test_item_1_3_route_approve_requires_confirmation(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)

    login(user.email, "senha123")
    response = client.post(
        f"/admin/pedidos/revisao/{order.id}/aprovar",
        data={},
        follow_redirects=True,
    )

    assert "Confirme a aprovação" in response.get_data(as_text=True)
    order = _reload(db, order)
    assert order.status == "manual_review"


def test_item_1_3_route_reject_requires_reason_and_confirmation(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)

    login(user.email, "senha123")
    without_reason = client.post(
        f"/admin/pedidos/revisao/{order.id}/rejeitar",
        data={"confirm_reject": "1"},
        follow_redirects=True,
    )
    assert "Informe o motivo" in without_reason.get_data(as_text=True)

    without_confirm = client.post(
        f"/admin/pedidos/revisao/{order.id}/rejeitar",
        data={"reason": "não confere"},
        follow_redirects=True,
    )
    assert "Confirme a rejeição" in without_confirm.get_data(as_text=True)

    order = _reload(db, order)
    assert order.status == "manual_review"
    assert order.stock_reserved is True


def test_item_1_3_route_reject_flow(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=10)
    order = _manual_order(db, user, product, quantity=2, reserved=True)

    login(user.email, "senha123")
    response = client.post(
        f"/admin/pedidos/revisao/{order.id}/rejeitar",
        data={"confirm_reject": "1", "reason": "valor divergente confirmado"},
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert "Pedido recusado" in html
    order = _reload(db, order)
    assert order.status == "cancelled"
    assert order.manual_review_decision == "rejected"
    assert order.manual_review_decided_reason == "valor divergente confirmado"
    assert product.stock == 10


def test_item_1_3_result_page_shows_rejected_text(
    client, db, make_user, make_product, login
):
    user = make_user()
    product = make_product(stock=10)
    order = _manual_order(db, user, product, reserved=True)
    order.status = "cancelled"
    order.manual_review_decision = "rejected"
    db.session.commit()

    login(user.email, "senha123")
    html = client.get(
        f"/pagamentos/retorno/falha?order_id={order.id}"
    ).get_data(as_text=True)

    assert "Pedido recusado" in html
    assert "Fale com o suporte para detalhes." in html
