"""Issue #89: fallback de janela deduplica apenas pedido ainda em andamento.

POST sem campo e sem GET (deep-link, script, sessão nova) que cai no
fallback de janela e bate num pedido do MESMO usuário com o MESMO
formulário na mesma janela de 30s:

- pedido ainda ``pending_payment`` (ou ``manual_review``) → reenvio é
  retry/duplo clique → 1 pedido (comportamento da trava do PR #88);
- pedido já concluído (``paid``/``expired``/``cancelled``) → segunda
  compra idêntica legítima → chave nova e 2º pedido (regra aprovada na
  issue #89);
- caminhos ``field``/``session`` (chave emitida por renderização) não
  mudam: a dedup continua incondicional.
"""

import pytest

from test_checkout_idempotency import (
    MP_URL_PREFIX,
    _freeze_endpoint_time,
    _login,
    _patch_shipping,
    _product_form,
    _recording_preference,
)

from loja.modules.getway_mercado_pago.payment_models import Order


def _setup(client, make_user, make_product, monkeypatch, *, stock=5):
    user = make_user()
    product = make_product(stock=stock)
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))
    _freeze_endpoint_time(monkeypatch, pr)
    return user, product, calls


def _force_status(db, user, status):
    order = Order.query.filter_by(user_id=user.id).one()
    order.status = status
    db.session.commit()
    return order


def test_issue_89_fallback_resend_pending_still_dedups(
    client, db, make_user, make_product, monkeypatch
):
    """Trava do comportamento: retry com pedido ainda pendente = 1 pedido."""
    user, product, calls = _setup(client, make_user, make_product, monkeypatch)

    first = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )
    second = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )

    assert first.status_code == 302
    assert second.status_code == 302
    assert second.headers["Location"] == first.headers["Location"]
    assert len(calls) == 1
    orders = Order.query.filter_by(user_id=user.id).all()
    assert len(orders) == 1
    assert orders[0].status == "pending_payment"


@pytest.mark.parametrize("terminal_status", ["paid", "expired", "cancelled"])
def test_issue_89_fallback_identical_form_after_concluded_creates_second_order(
    client, db, make_user, make_product, monkeypatch, terminal_status
):
    """Pedido já concluído na mesma janela não é reapresentado: 2 pedidos."""
    user, product, calls = _setup(client, make_user, make_product, monkeypatch)

    first = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )
    assert first.status_code == 302
    _force_status(db, user, terminal_status)

    second = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )

    assert second.status_code == 302
    assert second.headers["Location"].startswith(MP_URL_PREFIX)
    assert second.headers["Location"] != first.headers["Location"]

    orders = (
        Order.query
        .filter_by(user_id=user.id)
        .order_by(Order.created_at.asc())
        .all()
    )
    assert len(orders) == 2
    keys = [order.idempotency_key for order in orders]
    # Ambas caíram no fallback de janela (prefixo "w") e a nova chave é distinta.
    assert all(key and key.startswith("w") for key in keys)
    assert len(set(keys)) == 2
    assert orders[1].status == "pending_payment"
    assert len(calls) == 2


def test_issue_89_fallback_manual_review_still_dedups(
    client, db, make_user, make_product, monkeypatch
):
    """manual_review mantém a reserva ativa: reenvio segue em 1 pedido."""
    user, product, calls = _setup(client, make_user, make_product, monkeypatch)

    first = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )
    assert first.status_code == 302
    _force_status(db, user, "manual_review")

    second = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product),
    )

    assert second.status_code == 302
    assert second.headers["Location"] == first.headers["Location"]
    assert len(calls) == 1
    orders = Order.query.filter_by(user_id=user.id).all()
    assert len(orders) == 1
    assert orders[0].status == "manual_review"


def test_issue_89_field_key_resend_after_paid_still_redirects(
    client, db, make_user, make_product, monkeypatch
):
    """Caminho field (chave por renderização) não muda: dedup incondicional."""
    user, product, calls = _setup(client, make_user, make_product, monkeypatch)
    field_key = "fieldkey12345678"

    first = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product, key=field_key),
    )
    assert first.status_code == 302
    _force_status(db, user, "paid")

    second = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product, key=field_key),
    )

    assert second.status_code == 302
    assert second.headers["Location"] == first.headers["Location"]
    assert len(calls) == 1
    orders = Order.query.filter_by(user_id=user.id).all()
    assert len(orders) == 1
