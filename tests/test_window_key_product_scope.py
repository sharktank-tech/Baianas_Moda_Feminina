"""Resposta 1 / item 1.2: a proteção de janela NÃO bloqueia produtos diferentes.

Dois caminhos de resolução da chave, mesmo usuário, mesma janela de 30s
(relógio congelado — pior caso):

1. fluxo normal (renderização do checkout emite chave na sessão): comprar
   o produto A e em seguida o produto B gera 2 chaves emitidas distintas
   (a lista da sessão guarda a mais recente em primeiro lugar) → 2 pedidos;
2. fallback de janela (POST sem campo E sem GET prévio): a chave é
   sha256(user_id | bucket | formulário COMPLETO) — product_id e
   selected_size entram no digest → formulários diferentes = chaves
   diferentes → 2 pedidos.

Controle negativo (já coberto pela suíte): mesmo formulário duas vezes na
mesma janela = 1 pedido
(test_checkout_idempotency.py::test_post_without_key_and_without_get_is_protected_by_window_key).
"""

from test_checkout_idempotency import (
    MP_URL_PREFIX,
    _freeze_endpoint_time,
    _extract_key,
    _login,
    _patch_shipping,
    _product_form,
    _recording_preference,
)

from loja.modules.getway_mercado_pago.payment_models import Order


def _set_checkout_product(client, product):
    with client.session_transaction() as sess:
        sess["checkout_product"] = {
            "product_id": str(product.id),
            "size": "M",
            "quantity": 1,
            "variant_id": None,
        }


def test_two_products_same_window_normal_flow_create_two_orders(
    client, db, make_user, make_product, monkeypatch
):
    """Item 1.2 (caminho sessão): renderizar A, comprar A, renderizar B,
    comprar B — tudo na mesma janela de 30s — cria 2 pedidos."""
    user = make_user()
    product_a = make_product()
    product_b = make_product()
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))
    _freeze_endpoint_time(monkeypatch, pr)

    # Compra do produto A (GET emite chave na sessão; POST omite o campo).
    _set_checkout_product(client, product_a)
    resp_a = client.get(f"/checkout/{product_a.id}")
    assert resp_a.status_code == 200
    key_a = _extract_key(resp_a.get_data(as_text=True))
    assert key_a
    post_a = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product_a),
    )
    assert post_a.status_code == 302
    assert post_a.headers["Location"].startswith(MP_URL_PREFIX)

    # Compra do produto B em seguida (relógio congelado: mesma janela).
    _set_checkout_product(client, product_b)
    resp_b = client.get(f"/checkout/{product_b.id}")
    assert resp_b.status_code == 200
    key_b = _extract_key(resp_b.get_data(as_text=True))
    assert key_b and key_b != key_a
    post_b = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product_b),
    )
    assert post_b.status_code == 302
    assert post_b.headers["Location"].startswith(MP_URL_PREFIX)

    orders = Order.query.filter_by(user_id=user.id).all()
    assert len(orders) == 2
    assert {order.idempotency_key for order in orders} == {key_a, key_b}
    assert len(calls) == 2


def test_two_products_same_window_fallback_without_get_create_two_orders(
    client, db, make_user, make_product, monkeypatch
):
    """Item 1.2 (caminho fallback): POST direto sem GET nem campo,
    produto A e depois produto B na MESMA janela → 2 pedidos, chaves de
    janela distintas (digest inclui o formulário completo)."""
    user = make_user()
    product_a = make_product()
    product_b = make_product()
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))
    _freeze_endpoint_time(monkeypatch, pr)

    post_a = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product_a),
    )
    assert post_a.status_code == 302

    post_b = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product_b),
    )
    assert post_b.status_code == 302
    assert post_b.headers["Location"].startswith(MP_URL_PREFIX)

    orders = Order.query.filter_by(user_id=user.id).all()
    assert len(orders) == 2
    keys = [order.idempotency_key for order in orders]
    # Ambas caíram no fallback de janela (prefixo "w") e são distintas:
    # o digest do formulário de B difere do de A (product_id no digest).
    assert all(key and key.startswith("w") for key in keys)
    assert len(set(keys)) == 2
    assert len(calls) == 2
    assert len({call for call in calls}) == 2
