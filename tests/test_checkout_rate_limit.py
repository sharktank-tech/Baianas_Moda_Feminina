"""Item 1.8: limite de tentativas de checkout por usuário (janela fixa).

Toda tentativa que chega à criação do pedido em
``/pagamentos/mercado-pago`` (após o pre-check idempotente) consome 1
tentativa; ao estourar a janela a rota responde com o flash
``CHECKOUT_BLOCK_MESSAGE`` e NÃO cria pedido nem reserva estoque.
``CHECKOUT_RATE_LIMIT=0`` desliga o gate.
"""

from datetime import timedelta

from test_checkout_idempotency import (
    MP_URL_PREFIX,
    _login,
    _patch_shipping,
    _product_form,
    _recording_preference,
)

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import AuthRateLimit, utcnow
from loja.services.checkout_rate_limit import CHECKOUT_BLOCK_MESSAGE
from loja.services.rate_limits import _key_hash


def _set_limit(monkeypatch, app, limit, window=None):
    monkeypatch.setitem(app.config, "CHECKOUT_RATE_LIMIT", str(limit))
    if window is not None:
        monkeypatch.setitem(
            app.config, "CHECKOUT_RATE_WINDOW_SECONDS", str(window)
        )


def _flashes(client):
    with client.session_transaction() as sess:
        return list(sess.get("_flashes", []))


def _clear_flashes(client):
    with client.session_transaction() as sess:
        sess["_flashes"] = []


def _invalid_form(user, product):
    # "nome" obrigatório ausente → ValueError na validação; a tentativa
    # ainda é consumida pelo limite (o check roda depois do pre-check e
    # antes da criação — sem pedido existente, sempre passa por ele).
    form = _product_form(user, product)
    form.pop("nome")
    return form


def test_item_1_8_blocks_after_limit_and_spares_other_users(
    client, app, db, make_user, make_product, monkeypatch
):
    _set_limit(monkeypatch, app, 2)
    user_a = make_user()
    user_b = make_user()
    product = make_product()

    _login(client, user_a)
    _clear_flashes(client)

    for _ in range(2):
        resp = client.post(
            "/pagamentos/mercado-pago", data=_invalid_form(user_a, product)
        )
        assert resp.status_code == 302
    blocked = [
        message
        for category, message in _flashes(client)
        if category == "warning" and message == CHECKOUT_BLOCK_MESSAGE
    ]
    assert blocked == []

    resp = client.post(
        "/pagamentos/mercado-pago", data=_invalid_form(user_a, product)
    )
    assert resp.status_code == 302
    flashes = _flashes(client)
    assert ("warning", CHECKOUT_BLOCK_MESSAGE) in flashes
    # As 2 primeiras tentativas erraram na validação; a 3ª foi bloqueada
    # antes de validar.
    assert [m for c, m in flashes if c == "danger"] == [
        "Informe o nome completo.",
        "Informe o nome completo.",
    ]

    # Outro usuário não é afetado pela janela do usuário A.
    client_b = app.test_client()
    _login(client_b, user_b)
    _clear_flashes(client_b)
    resp_b = client_b.post(
        "/pagamentos/mercado-pago", data=_invalid_form(user_b, product)
    )
    assert resp_b.status_code == 302
    assert ("warning", CHECKOUT_BLOCK_MESSAGE) not in _flashes(client_b)


def test_item_1_8_blocked_attempt_creates_no_order(
    client, app, db, make_user, make_product, monkeypatch
):
    _set_limit(monkeypatch, app, 1)
    user = make_user()
    product_a = make_product()
    product_b = make_product()
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))
    _login(client, user)
    _clear_flashes(client)

    first = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product_a),
    )
    assert first.status_code == 302
    assert first.headers["Location"].startswith(MP_URL_PREFIX)
    assert len(calls) == 1

    # 2ª tentativa, produto DIFERENTE: sem o limite criaria o 2º pedido
    # (formulários distintos não colidem no fallback de janela).
    second = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product_b),
    )
    assert second.status_code == 302
    assert ("warning", CHECKOUT_BLOCK_MESSAGE) in _flashes(client)

    orders = Order.query.filter_by(user_id=user.id).all()
    assert len(orders) == 1
    assert len(calls) == 1


def test_item_1_8_window_resets_after_elapsed(
    client, app, db, make_user, make_product, monkeypatch
):
    _set_limit(monkeypatch, app, 1, window=300)
    user = make_user()
    product = make_product()
    _login(client, user)
    _clear_flashes(client)

    client.post(
        "/pagamentos/mercado-pago", data=_invalid_form(user, product)
    )
    client.post(
        "/pagamentos/mercado-pago", data=_invalid_form(user, product)
    )
    assert ("warning", CHECKOUT_BLOCK_MESSAGE) in _flashes(client)

    row = (
        db.session.query(AuthRateLimit)
        .filter_by(scope="checkout", key_hash=_key_hash(str(user.id)))
        .one()
    )
    row.window_started_at = utcnow() - timedelta(seconds=301)
    db.session.commit()

    resp = client.post(
        "/pagamentos/mercado-pago", data=_invalid_form(user, product)
    )
    assert resp.status_code == 302
    warnings = [
        message
        for category, message in _flashes(client)
        if category == "warning"
    ]
    assert warnings == [CHECKOUT_BLOCK_MESSAGE]
    # A 3ª tentativa passou pela validação (danger novo depois do reset);
    # sem o reset ela teria sido bloqueada e os dangers ficariam em 1.
    assert [m for c, m in _flashes(client) if c == "danger"].count(
        "Informe o nome completo."
    ) == 2


def test_item_1_8_zero_limit_disables_gate(
    client, app, db, make_user, make_product, monkeypatch
):
    _set_limit(monkeypatch, app, 0)
    user = make_user()
    product = make_product()
    _login(client, user)
    _clear_flashes(client)

    for _ in range(3):
        resp = client.post(
            "/pagamentos/mercado-pago", data=_invalid_form(user, product)
        )
        assert resp.status_code == 302

    assert ("warning", CHECKOUT_BLOCK_MESSAGE) not in _flashes(client)
    # Gate OFF não toca no banco.
    assert (
        db.session.query(AuthRateLimit)
        .filter_by(scope="checkout", key_hash=_key_hash(str(user.id)))
        .count()
        == 0
    )
