"""Item 1.1 (guarda de regressão): o checkout SEMPRE grava o prazo da reserva.

Todo pedido criado pelo endpoint POST /pagamentos/mercado-pago nasce com
mp_preference_expires_at = created_at + 24h (DEFAULT_RESERVATION_HOURS).
Nada de novas linhas NULL — foi exatamente essa classe de linhas legadas
(pre-2026-09-21) que travou o estoque para sempre e motivou o backfill da
migration 0030 (testes em tests/test_reservation_expiry_backfill_pg.py).
"""

from datetime import timedelta

from test_checkout_idempotency import (
    _login,
    _patch_shipping,
    _product_form,
    _recording_preference,
)

from loja.services.reservation_policy import DEFAULT_RESERVATION_HOURS


def test_checkout_order_gets_reservation_expiry(
    client, db, make_user, make_product, monkeypatch
):
    """Item 1.1: pedido criado pelo checkout tem prazo persistido e correto."""
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.utils import utcnow

    user = make_user()
    product = make_product()
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))

    response = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )
    assert response.status_code in (302, 200)

    order = (
        Order.query.filter_by(user_id=user.id)
        .order_by(Order.created_at.desc())
        .first()
    )
    assert order is not None
    assert order.mp_preference_expires_at is not None
    assert (
        order.mp_preference_expires_at
        == order.created_at
        + timedelta(hours=DEFAULT_RESERVATION_HOURS)
    )
    # Sempre não nulo (é a classe de defeito do item 1.1) e não no futuro
    # absurdo: nasce a partir do próprio created_at do pedido.
    assert order.mp_preference_expires_at >= utcnow() - timedelta(minutes=1)
    assert calls == [order.id]
