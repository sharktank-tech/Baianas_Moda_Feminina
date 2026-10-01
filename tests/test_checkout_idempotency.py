"""Idempotência do checkout (item 1.7): duplo clique/F5 não duplica pedido.

Cobre POST /pagamentos/mercado-pago com checkout_type=product e =cart:

- a página de checkout renderiza o hidden field ``idempotency_key`` (uma
  chave por renderização);
- reenvio sequencial (F5 / voltar do Mercado Pago) reaproveita o pedido já
  criado em vez de criar outro ou mostrar erro;
- duas requisições CONCORRENTES reais (threads, sobreposição determinística)
  terminam com 1 pedido, 1 reserva de estoque e 1 chamada de preferência;
- a proteção de camada de segurança é a constraint UNIQUE
  (user_id, idempotency_key) no banco, não o pre-check em memória;
- o fluxo cart continua funcionando (primeiro POST cria e esvazia o
  carrinho; o reenvio reaproveita o pedido);
- formulário legado sem chave continua aceitando pedido (degradação graciosa);
- checkout de convidado não existe: anônimo é redirecionado ao login.

A corrida real em PostgreSQL (bloqueio observado em pg_stat_activity) fica em
tests/test_checkout_idempotency_pg.py.
"""

import logging
import re
import sqlite3
import threading
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from loja.modules.getway_mercado_pago.payment_models import Order

MP_URL_PREFIX = "https://mp.example/checkout"


def _product_form(user, product, key=None, quantity=1):
    form = {
        "checkout_type": "product",
        "product_id": str(product.id),
        "quantity": str(quantity),
        "selected_size": "M",
        "nome": "Cliente Teste",
        "email": user.email,
        "cpf": "52998224725",
        "cep": "01310100",
        "estado": "SP",
        "endereco": "Rua Teste",
        "numero": "10",
        "bairro": "Centro",
        "cidade": "São Paulo",
    }
    if key is not None:
        form["idempotency_key"] = key
    return form


def _cart_form(user, key=None):
    form = {
        "checkout_type": "cart",
        "nome": "Cliente Teste",
        "email": user.email,
        "cpf": "52998224725",
        "cep": "01310100",
        "estado": "SP",
        "endereco": "Rua Teste",
        "numero": "10",
        "bairro": "Centro",
        "cidade": "São Paulo",
    }
    if key is not None:
        form["idempotency_key"] = key
    return form


def _login(client, user):
    return client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )


def _capture_app_logs(logger):
    """Grava mensagens do app.logger em uma lista (padrão da suíte)."""
    captured = []

    class Recorder(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Recorder()
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    def _stop():
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    return captured, _stop


def _extract_key(html):
    match = re.search(r'name="idempotency_key"\s+value="([^"]+)"', html)
    return match.group(1) if match else None


def _patch_shipping(monkeypatch):
    import loja.views.payments_routes as pr

    monkeypatch.setattr(
        pr, "quote_checkout_shipping",
        lambda item_data, cep: Decimal("10.00"),
    )
    return pr


def _recording_preference(calls):
    """Preferência falsa com ID/url únicos (mp_preference_id é UNIQUE e a
    base SQLite é compartilhada por toda a suíte)."""

    def _pref(order):
        token = uuid.uuid4().hex[:12]
        calls.append(order.id)
        return {
            "preference_id": f"pref-{token}",
            "checkout_url": f"{MP_URL_PREFIX}/{token}",
        }

    return _pref


def _make_cart(db, user, product, quantity=1):
    from loja.modules.models import Cart, CartItem

    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(CartItem(
        id=uuid.uuid4(),
        cart_id=cart.id,
        product_id=product.id,
        selected_size="M",
        quantity=quantity,
        unit_price=product.price,
    ))
    db.session.commit()
    return cart


def _order_payload(user, **extra):
    data = dict(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=Decimal("49.90"),
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=Decimal("59.90"),
        stock_reserved=True,
        customer_name="Cliente Teste",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    data.update(extra)
    return data


# ── Página de checkout renderiza a chave ─────────────────────────────────────

def test_checkout_page_renders_idempotency_key(
    client, db, make_user, make_product,
):
    """Item 1.7: hidden field idempotency_key nas duas renderizações."""
    user = make_user()
    product = make_product()
    _login(client, user)
    _make_cart(db, user, product)

    resp_cart = client.get("/checkout/carrinho")
    assert resp_cart.status_code == 200
    key_cart = _extract_key(resp_cart.get_data(as_text=True))
    assert key_cart and len(key_cart) == 32

    with client.session_transaction() as sess:
        sess["checkout_product"] = {
            "product_id": str(product.id),
            "size": "M",
            "quantity": 1,
            "variant_id": None,
        }
    resp_product = client.get(f"/checkout/{product.id}")
    assert resp_product.status_code == 200
    key_product = _extract_key(resp_product.get_data(as_text=True))
    assert key_product and len(key_product) == 32

    # Cada renderização gera chave nova: compra legítima posterior não é
    # bloqueada pelo pedido anterior.
    assert key_product != key_cart


# ── Reenvio sequencial (F5 / voltar do Mercado Pago) ─────────────────────────

def test_sequential_resubmit_redirects_to_existing_order(
    client, db, make_user, make_product, monkeypatch,
):
    """Item 1.7: 2º POST com a mesma chave reaproveita o pedido (não erro)."""
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    key = uuid.uuid4().hex

    captured, stop_capture = _capture_app_logs(client.application.logger)
    try:
        first = client.post(
            "/pagamentos/mercado-pago",
            data=_product_form(user, product, key),
        )
        assert first.status_code == 302
        assert first.headers["Location"].startswith(MP_URL_PREFIX)

        second = client.post(
            "/pagamentos/mercado-pago",
            data=_product_form(user, product, key),
        )
        assert second.status_code == 302
        assert second.headers["Location"] == first.headers["Location"]
    finally:
        stop_capture()

    # Prova o mecanismo: o 2º POST parou no pre-check, não em erro.
    assert any("pre-check" in message for message in captured)

    # Prova de reaproveitamento: 1 pedido, 1 preferência, 1 reserva.
    assert len(calls) == 1
    orders = (
        db.session.query(Order)
        .filter_by(user_id=user.id)
        .all()
    )
    assert len(orders) == 1
    assert orders[0].idempotency_key == key
    assert orders[0].id == calls[0]
    db.session.refresh(product)
    assert product.stock == 4


# ── Corrida real: duas requisições concorrentes em threads ───────────────────

def test_concurrent_double_submit_creates_single_order(
    app, db, make_user, make_product, monkeypatch,
):
    """Item 1.7: duplo clique CONCORRENTE -> 1 pedido, 1 reserva, 1 preferência.

    Sobreposição determinística: o vencedor para dentro de create_preference
    (flush feito, transação ainda ABERTA) e só termina depois que o perdedor
    provou ter passado pelo pre-check sem encontrar pedido (ou seja, as duas
    requisições estão vivas ao mesmo tempo). O perdedor então bate na
    constraint UNIQUE, faz rollback da própria reserva e é redirecionado ao
    pedido do vencedor.

    Journal WAL durante o teste: no journal de rollback clássico o COMMIT do
    vencedor colide com o SHARED retido pelo perdedor em busy-retry e o
    próprio COMMIT recebe "database is locked" — um impasse known do SQLite,
    não do código do checkout.
    """
    pr = _patch_shipping(monkeypatch)
    user = make_user()
    product = make_product(stock=5)
    key = uuid.uuid4().hex

    calls = []
    gating_errors = []
    winner_in_txn = threading.Event()
    loser_missed_precheck = threading.Event()

    original_find = pr._find_duplicate_checkout

    def tracked_find(candidate):
        result = original_find(candidate)
        if (
            threading.current_thread().name == "loser"
            and result is None
        ):
            loser_missed_precheck.set()
        return result

    monkeypatch.setattr(
        pr, "_find_duplicate_checkout", tracked_find,
    )

    def gated_preference(order):
        calls.append(order.id)
        if threading.current_thread().name != "winner":
            gating_errors.append(
                "create_preference chamado por thread errada"
            )
        winner_in_txn.set()
        if not loser_missed_precheck.wait(timeout=10):
            gating_errors.append(
                "perdedor não passou pelo pre-check com o vencedor "
                "sem commitar"
            )
        time.sleep(0.2)
        return {
            "preference_id": "pref-race",
            "checkout_url": f"{MP_URL_PREFIX}/race",
        }

    monkeypatch.setattr(pr, "create_preference", gated_preference)

    db_path = db.engine.url.database
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
    finally:
        raw.close()

    results = {}
    errors = []
    form = _product_form(user, product, key)

    winner_client = app.test_client()
    loser_client = app.test_client()
    _login(winner_client, user)
    _login(loser_client, user)

    def run_winner():
        try:
            resp = winner_client.post(
                "/pagamentos/mercado-pago", data=form,
            )
            results["winner"] = (
                resp.status_code,
                resp.headers.get("Location"),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(("winner", repr(exc)))

    def run_loser():
        try:
            if not winner_in_txn.wait(timeout=10):
                raise AssertionError(
                    "vencedor não entrou na transação"
                )
            resp = loser_client.post(
                "/pagamentos/mercado-pago", data=form,
            )
            results["loser"] = (
                resp.status_code,
                resp.headers.get("Location"),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(("loser", repr(exc)))

    winner_thread = threading.Thread(
        target=run_winner, name="winner",
    )
    loser_thread = threading.Thread(
        target=run_loser, name="loser",
    )
    captured, stop_capture = _capture_app_logs(app.logger)
    try:
        loser_thread.start()
        winner_thread.start()
        winner_thread.join(timeout=30)
        loser_thread.join(timeout=30)
    finally:
        stop_capture()

    try:
        assert not winner_thread.is_alive()
        assert not loser_thread.is_alive()
        assert errors == [], errors
        assert gating_errors == [], gating_errors

        # Prova o mecanismo: o perdedor passou pelo pre-check (miss) e só
        # reaproveitou o pedido depois que a constraint UNIQUE recusou o
        # próprio INSERT — caminho IntegrityError, não validação.
        assert any(
            "constraint UNIQUE recusou o 2o INSERT" in message
            for message in captured
        ), captured

        # As duas respostas levam ao MESMO pagamento; nenhuma caiu no
        # redirecionamento de erro ("/" ou referrer).
        assert results["winner"] == (
            302, f"{MP_URL_PREFIX}/race",
        )
        assert results["loser"] == (
            302, f"{MP_URL_PREFIX}/race",
        )

        # 1 preferência real (só o vencedor chegou a criar),
        # 1 pedido com a chave, 1 débito de estoque.
        assert len(calls) == 1
        orders = (
            db.session.query(Order)
            .filter_by(user_id=user.id)
            .all()
        )
        assert len(orders) == 1
        assert orders[0].idempotency_key == key
        assert orders[0].id == calls[0]
        db.session.refresh(product)
        assert product.stock == 4
    finally:
        # Restaura o journal para não afetar os demais testes da suíte:
        # rollback encerra a transação de leitura da sessão principal,
        # dispose() fecha as conexões em pool que ainda estão no WAL.
        db.session.rollback()
        db.engine.dispose()
        raw = sqlite3.connect(db_path, timeout=10)
        try:
            raw.execute("PRAGMA journal_mode=DELETE")
        finally:
            raw.close()


# ── Carrinho ─────────────────────────────────────────────────────────────────

def test_cart_resubmit_redirects_to_existing_order(
    client, db, make_user, make_product, monkeypatch,
):
    """Item 1.7: checkout_type=cart não quebra — cria, esvazia e reaproveita."""
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))

    user = make_user()
    product = make_product(stock=5)
    cart = _make_cart(db, user, product)
    _login(client, user)
    key = uuid.uuid4().hex

    captured, stop_capture = _capture_app_logs(client.application.logger)
    try:
        first = client.post(
            "/pagamentos/mercado-pago",
            data=_cart_form(user, key),
        )
        assert first.status_code == 302
        assert first.headers["Location"].startswith(MP_URL_PREFIX)

        # O primeiro POST esvazia o carrinho (fluxo cart intacto).
        db.session.refresh(cart)
        assert cart.items == []

        # Reenvio: reaproveita o pedido em vez de falhar com
        # "Seu carrinho está vazio".
        second = client.post(
            "/pagamentos/mercado-pago",
            data=_cart_form(user, key),
        )
        assert second.status_code == 302
        assert second.headers["Location"] == first.headers["Location"]
    finally:
        stop_capture()

    assert any("pre-check" in message for message in captured)

    orders = (
        db.session.query(Order)
        .filter_by(user_id=user.id)
        .all()
    )
    assert len(orders) == 1
    assert orders[0].checkout_type == "cart"
    assert orders[0].idempotency_key == key
    assert len(calls) == 1
    db.session.refresh(product)
    assert product.stock == 4


def test_resubmit_without_checkout_url_falls_back_to_order_page(
    client, db, make_user, monkeypatch,
):
    """Item 1.7: pedido reutilizado sem URL do MP cai na página do pedido."""
    _patch_shipping(monkeypatch)
    user = make_user()
    _login(client, user)
    key = uuid.uuid4().hex

    db.session.add(Order(
        **_order_payload(user, idempotency_key=key, mp_checkout_url=None),
    ))
    db.session.commit()

    # Pre-check responde antes de o endpoint validar o produto.
    form = _product_form(user, type("P", (), {"id": uuid.uuid4()})(), key)
    resp = client.post("/pagamentos/mercado-pago", data=form)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/encomendas/pedido/")


# ── Chaves distintas e formulários legados ───────────────────────────────────

def test_distinct_idempotency_keys_create_separate_orders(
    client, db, make_user, make_product, monkeypatch,
):
    """Item 1.7: compra legítima posterior (chave nova) NÃO é bloqueada."""
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)

    first = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product, uuid.uuid4().hex),
    )
    second = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product, uuid.uuid4().hex),
    )
    assert first.status_code == 302
    assert second.status_code == 302
    assert first.headers["Location"].startswith(MP_URL_PREFIX)
    assert second.headers["Location"].startswith(MP_URL_PREFIX)
    assert first.headers["Location"] != second.headers["Location"]
    assert len(calls) == 2
    orders = (
        db.session.query(Order)
        .filter_by(user_id=user.id)
        .all()
    )
    assert len(orders) == 2
    assert orders[0].idempotency_key != orders[1].idempotency_key
    db.session.refresh(product)
    assert product.stock == 3


def test_form_without_idempotency_key_keeps_legacy_behavior(
    client, db, make_user, make_product, monkeypatch,
):
    """Item 1.7: página legada sem hidden field não quebra o checkout."""
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)

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
    assert first.headers["Location"].startswith(MP_URL_PREFIX)
    assert second.headers["Location"].startswith(MP_URL_PREFIX)
    assert len(calls) == 2
    orders = (
        db.session.query(Order)
        .filter_by(user_id=user.id)
        .all()
    )
    assert len(orders) == 2
    assert [o.idempotency_key for o in orders] == [None, None]


def test_invalid_idempotency_key_is_ignored(
    client, db, make_user, make_product, monkeypatch,
):
    """Item 1.7: chave malformada não trava o checkout (fail-open no formato)."""
    pr = _patch_shipping(monkeypatch)
    calls = []
    monkeypatch.setattr(pr, "create_preference", _recording_preference(calls))

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)

    resp = client.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product, "x" * 65),
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith(MP_URL_PREFIX)
    orders = (
        db.session.query(Order)
        .filter_by(user_id=user.id)
        .all()
    )
    assert len(orders) == 1
    assert orders[0].idempotency_key is None


# ── Convidado e constraint do banco ──────────────────────────────────────────

def test_anonymous_post_is_redirected_to_login_no_order(
    app, db, make_user, make_product,
):
    """Item 1.7 (1.2): checkout de convidado NÃO existe — @login_required."""
    user = make_user()
    product = make_product(stock=5)
    anon = app.test_client()

    resp = anon.post(
        "/pagamentos/mercado-pago",
        data=_product_form(user, product, uuid.uuid4().hex),
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert (
        db.session.query(Order).filter_by(user_id=user.id).count() == 0
    )


def test_database_unique_constraint_rejects_duplicate_key(
    db, make_user,
):
    """Item 1.7: a camada de segurança é a constraint UNIQUE no banco.

    Vale mesmo se o pre-check em memória falhar (corrida): o segundo INSERT
    com a mesma (user_id, idempotency_key) é recusado pelo PostgreSQL/SQLite.
    NULL x NULL não colide (pedidos legados continuam válidos).
    """
    user = make_user()
    key = "dupkey-1234567890"

    db.session.add(Order(**_order_payload(user, idempotency_key=key)))
    db.session.commit()

    db.session.add(Order(**_order_payload(user, idempotency_key=key)))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    # Legados sem chave nunca colidem entre si.
    db.session.add(Order(**_order_payload(user, idempotency_key=None)))
    db.session.add(Order(**_order_payload(user, idempotency_key=None)))
    db.session.commit()
    assert (
        db.session.query(Order).filter_by(user_id=user.id).count() == 3
    )
