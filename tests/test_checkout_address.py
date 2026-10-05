"""Endereço no checkout — pendências #1 ("None" no complemento) e #2
(endereço não salvo em "Meu Perfil → Endereços").

#1: ``Address.complement``/``phone`` são nullable e o template imprimia o
    Python ``None`` como texto literal "None" no input do checkout.
#2: o checkout só gravava snapshot nas colunas do pedido — nenhum
    ``Address`` era criado/atualizado; o cadastro ficava vazio e o
    checkout reaberto não pré-preenchia nada.
#2b: o bloco "Entrega" do detalhe do pedido depende de
    ``Order.shipping_address`` como mapping (property).
"""

import re
import uuid

from test_checkout_idempotency import (
    MP_URL_PREFIX,
    _extract_key,
    _freeze_endpoint_time,
    _login,
    _patch_shipping,
    _product_form,
    _recording_preference,
)

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import Address


def _set_checkout_product(client, product):
    with client.session_transaction() as sess:
        sess["checkout_product"] = {
            "product_id": str(product.id),
            "size": "M",
            "quantity": 1,
            "variant_id": None,
        }


def _make_address(db, user, **overrides):
    fields = dict(
        id=uuid.uuid4(),
        user_id=user.id,
        recipient_name="Ana Silva",
        zip_code="01310-100",
        street="Rua Velha",
        number="1",
        complement=None,
        phone=None,
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        is_default=True,
    )
    fields.update(overrides)
    address = Address(**fields)
    db.session.add(address)
    db.session.commit()
    return address


def test_checkout_renders_no_none_in_address_fields(
    client, db, make_user, make_product
):
    """P1 — endereço com complement/phone NULL e usuário sem telefone:
    o HTML do checkout não pode conter o texto literal 'None'."""
    user = make_user()
    user.phone = None
    db.session.commit()
    _make_address(db, user)
    product = make_product()
    _login(client, user)
    _set_checkout_product(client, product)

    resp = client.get(f"/checkout/{product.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "value=\"None\"" not in html, "P1: literal 'None' no formulário"
    m = re.search(r'name="complemento"[^>]*value="([^"]*)"', html, re.S)
    assert m and m.group(1) == "", m and m.group(1)
    m = re.search(r'name="telefone"[^>]*value="([^"]*)"', html, re.S)
    assert m and m.group(1) == "", m and m.group(1)


def test_checkout_creates_first_address_and_prefills(
    client, db, make_user, make_product, monkeypatch
):
    """P2 — primeiro checkout cria o Address (default) e a reabertura
    pré-preenche os campos a partir do cadastro."""
    user = make_user()
    product = make_product()
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    monkeypatch.setattr(pr, "create_preference", _recording_preference([]))
    _freeze_endpoint_time(monkeypatch, pr)

    _set_checkout_product(client, product)
    resp = client.get(f"/checkout/{product.id}")
    key = _extract_key(resp.get_data(as_text=True))
    assert key

    form = _product_form(user, product, key=key)
    form["complemento"] = "Apto 42"
    post = client.post("/pagamentos/mercado-pago", data=form)
    assert post.status_code == 302
    assert post.headers["Location"].startswith(MP_URL_PREFIX)

    rows = Address.query.filter_by(user_id=user.id).all()
    assert len(rows) == 1, "P2: checkout não salvou o endereço"
    address = rows[0]
    assert address.is_default is True
    assert address.street == "Rua Teste"
    assert address.number == "10"
    assert address.complement == "Apto 42"
    assert address.recipient_name == "Cliente Teste"
    assert address.zip_code == "01310100"

    _set_checkout_product(client, product)
    html = client.get(f"/checkout/{product.id}").get_data(as_text=True)
    assert 'value="Rua Teste"' in html
    assert 'value="Apto 42"' in html


def test_checkout_updates_existing_default_address(
    client, db, make_user, make_product, monkeypatch
):
    """P2 — com endereço já cadastrado, o checkout atualiza o default
    (mesmo id, sem duplicar linha)."""
    user = make_user()
    product = make_product()
    existing = _make_address(db, user)
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    monkeypatch.setattr(pr, "create_preference", _recording_preference([]))
    _freeze_endpoint_time(monkeypatch, pr)

    _set_checkout_product(client, product)
    resp = client.get(f"/checkout/{product.id}")
    key = _extract_key(resp.get_data(as_text=True))

    form = _product_form(user, product, key=key)
    form["complemento"] = ""
    post = client.post("/pagamentos/mercado-pago", data=form)
    assert post.status_code == 302

    rows = Address.query.filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].id == existing.id, "P2: endereço default foi duplicado"
    assert rows[0].street == "Rua Teste"
    assert rows[0].complement is None, "complemento vazio deve virar NULL"


def test_order_detail_renders_shipping_address(
    client, db, make_user, make_product, monkeypatch
):
    """P2b — ``Order.shipping_address`` como mapping destrava o bloco
    'Entrega' do detalhe do pedido (antes: atributo Undefined, seção morta)."""
    user = make_user()
    product = make_product()
    _login(client, user)
    pr = _patch_shipping(monkeypatch)
    monkeypatch.setattr(pr, "create_preference", _recording_preference([]))
    _freeze_endpoint_time(monkeypatch, pr)

    _set_checkout_product(client, product)
    resp = client.get(f"/checkout/{product.id}")
    key = _extract_key(resp.get_data(as_text=True))
    form = _product_form(user, product, key=key)
    form["complemento"] = "Apto 42"
    assert client.post("/pagamentos/mercado-pago", data=form).status_code == 302

    order = Order.query.filter_by(user_id=user.id).one()
    detail = client.get(f"/encomendas/pedido/{order.id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert "Entrega" in html
    assert "Rua Teste" in html
    assert "Apto 42" in html
