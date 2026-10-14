"""Badge de quantidade da sacola: regras de exibição e fonte única.

- Soma das quantidades (2 produtos x 3 un. = 6); vazio oculta; >99 vira 99+.
- Fonte única: Cart persistido (autenticado) ou item pendente em sessão
  (visitante). Sem contagem paralela; sem tocar preço/estoque/checkout.
"""
import uuid

import pytest


def _helpers():
    from loja.views.routes import (
        cart_count_label,
        format_cart_count,
        get_cart_count,
    )
    return get_cart_count, format_cart_count, cart_count_label


@pytest.mark.parametrize("count,expected", [
    (0, ""), (-3, ""), (1, "1"), (6, "6"), (99, "99"),
    (100, "99+"), (250, "99+"), (None, ""), ("x", ""),
])
def test_format_cart_count(count, expected):
    _, format_cart_count, _ = _helpers()
    assert format_cart_count(count) == expected


@pytest.mark.parametrize("count,expected", [
    (0, "Sacola"), (1, "Carrinho com 1 item"), (3, "Carrinho com 3 itens"),
    (100, "Carrinho com 100 itens"),
])
def test_cart_count_label(count, expected):
    _, _, cart_count_label = _helpers()
    assert cart_count_label(count) == expected


def test_visitante_sem_pendente_conta_zero(client):
    get_cart_count, _, _ = _helpers()
    with client.application.test_request_context("/"):
        assert get_cart_count() == 0


def test_visitante_com_pendente_reutiliza_sessao(client):
    with client.session_transaction() as sess:
        sess["pending_cart_item"] = {"product_id": "x", "quantity": 2}
    response = client.get("/cart/count")
    assert response.status_code == 200
    assert response.get_json() == {
        "count": 2, "display": "2", "label": "Carrinho com 2 itens",
    }


def test_autenticado_soma_quantidades(client, db, make_user, make_product,
                                      login):
    from loja.modules.models import Cart, CartItem
    get_cart_count, _, _ = _helpers()
    user = make_user()
    other = make_user()
    p1, p2 = make_product(), make_product()
    for owner, quantities in ((user, (3, 3)), (other, (50,))):
        cart = Cart(id=uuid.uuid4(), user_id=owner.id)
        db.session.add(cart)
        db.session.flush()
        for product, qty in zip((p1, p2), quantities):
            db.session.add(CartItem(
                id=uuid.uuid4(), cart_id=cart.id, product_id=product.id,
                selected_size="M", quantity=qty, unit_price=product.price))
    db.session.commit()

    login(user.email)
    response = client.get("/cart/count")
    assert response.get_json()["count"] == 6
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert 'class="cart-badge"' in html
    assert ">6</span>" in html
    assert 'aria-label="Carrinho com 6 itens"' in html


def test_autenticado_vazio_oculta_badge(client, make_user, login):
    user = make_user()
    login(user.email)
    response = client.get("/cart/count")
    assert response.get_json() == {
        "count": 0, "display": "", "label": "Sacola",
    }
    html = client.get("/").get_data(as_text=True)
    assert 'class="cart-badge"' not in html
    assert 'aria-label="Sacola"' in html


def test_acima_de_99_exibe_99_mais(client, db, make_user, make_product, login):
    from loja.modules.models import Cart, CartItem
    user = make_user()
    product = make_product()
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(CartItem(
        id=uuid.uuid4(), cart_id=cart.id, product_id=product.id,
        selected_size="M", quantity=99, unit_price=product.price))
    db.session.add(CartItem(
        id=uuid.uuid4(), cart_id=cart.id, product_id=make_product().id,
        selected_size="M", quantity=5, unit_price=product.price))
    db.session.commit()
    login(user.email)
    assert client.get("/cart/count").get_json()["display"] == "99+"
    html = client.get("/").get_data(as_text=True)
    assert ">99+</span>" in html


def _js():
    from pathlib import Path
    return Path("public/static/scripts/cart_badge.js").read_text()


def test_js_atualiza_no_pageshow_sem_cssom():
    src = _js()
    assert "pageshow" in src and "persisted" in src
    assert "/cart/count" in src
    assert ".style." not in src  # CSP style-src-attr 'none'
    assert "updateCartBadge" in src


def test_css_badge_no_template():
    from pathlib import Path
    base = Path("loja/templates/base.html").read_text()
    assert ".cart-badge" in base
    assert "#2563eb" in base  # azul do projeto (info/admin)
    assert ".navbar-icon .cart-icon-wrap" in base  # visível no mobile
    assert "pointer-events: none" in base  # área clicável intacta
    assert "cart-icon-wrap" in base and "navbar-cart" in base
