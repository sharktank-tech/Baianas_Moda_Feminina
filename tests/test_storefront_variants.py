"""Testes do storefront multivariante AliExpress (Fase 7.2).

Cobrimos: opção canônica (`variant_id`) no carrinho/checkout/pedido, preço de
venda sempre `Product.price`, estoque por variante na reserva/liberação, a não
exposição de supplier_sku_id/supplier_product_id, e a renderização sanitizada da
descrição no storefront.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

import loja.views.payments_routes as pr
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import (
    Cart,
    CartItem,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)
from loja.utils import utcnow
from loja.views.routes import (
    _assert_selector_invariant,
    _build_variant_selector,
    _resolve_selected_variant,
    _uses_variants,
    _validate_cart_for_checkout,
)


@pytest.fixture(autouse=True)
def _cleanup_db(db):
    yield
    db.session.rollback()
    db.session.remove()


def _sku(id_, color, size, stock, price="80.00"):
    return ProductVariant(
        id=uuid.uuid4(),
        external_sku_id=id_,
        supplier_sku_id=id_,
        color=color,
        size=size,
        stock=stock,
        price=Decimal(price),
        sale_price=None,
        is_active=stock > 0,
    )


def make_aliexpress_product(db):
    supplier = Supplier(
        id=uuid.uuid4(),
        name="AliExpress Teste",
        channel_type="aliexpress",
        seller_id="test-seller",
        default_currency="BRL",
        is_sync_enabled=False,
    )
    db.session.add(supplier)
    db.session.flush()

    product = Product(
        id=uuid.uuid4(),
        name="Vestido Multivariante",
        description='<p>Bonito</p><script>alert("x")</script>',
        price=Decimal("142.86"),
        cost=Decimal("46.60"),
        image1="https://ae.example/img1.jpg",
        category="feminino",
        stock=9,
        is_active=True,
        is_draft=False,
        source="aliexpress",
        external_id="2000000000001",
        currency="BRL",
        target_margin_percent=Decimal("30.00"),
        pricing_status="priced",
    )
    db.session.add(product)
    db.session.flush()

    sp = SupplierProduct(
        id=uuid.uuid4(),
        supplier_id=supplier.id,
        product_id=product.id,
        supplier_product_id="2000000000001",
        cost=Decimal("46.60"),
        currency="BRL",
        freight_status="available",
        shipping_cost=Decimal("20.00"),
        landed_cost=Decimal("66.60"),
        freight_currency="BRL",
        is_active=True,
    )
    db.session.add(sp)
    db.session.flush()

    rosa_m = _sku("2000000000001-1", "Rosa", "M", 4)
    azul_m = _sku("2000000000001-2", "Azul", "M", 5)
    rosa_m.product_id = product.id
    rosa_m.supplier_product_id = sp.id
    azul_m.product_id = product.id
    azul_m.supplier_product_id = sp.id
    db.session.add_all([rosa_m, azul_m])
    db.session.commit()
    db.session.refresh(product)
    return product, rosa_m, azul_m


@pytest.fixture()
def ae_product(db):
    return make_aliexpress_product(db)


def _variant_item(product, variant, quantity=2):
    unit_price = Decimal(str(product.price))
    return [
        {
            "product_id": product.id,
            "product_name": product.name,
            "selected_size": "Rosa M",
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": unit_price * quantity,
            "variant_id": variant.id,
        }
    ]


def _make_variant_order(db, user, product, variant, quantity=2):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    unit_price = Decimal(str(product.price))
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=unit_price * quantity,
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=unit_price * quantity + Decimal("10.00"),
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
    order.items.append(
        OrderItem(
            id=uuid.uuid4(),
            product_id=product.id,
            product_name=product.name,
            selected_size="Rosa M",
            variant_id=variant.id,
            quantity=quantity,
            unit_price=unit_price,
            line_total=unit_price * quantity,
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


def _mp_payload(payment_id, order, status, detail):
    return {
        "id": payment_id,
        "external_reference": str(order.id),
        "transaction_amount": str(order.total),
        "currency_id": "BRL",
        "status": status,
        "status_detail": detail,
    }


# ── Seleção de variante ──────────────────────────────────────────────────────

def test_uses_variants_true_for_aliexpress(ae_product):
    product, _, _ = ae_product
    assert _uses_variants(product) is True


def test_resolve_selected_variant_ok(ae_product):
    product, rosa_m, _ = ae_product
    variant = _resolve_selected_variant(product, str(rosa_m.id))
    assert variant.id == rosa_m.id


def test_resolve_selected_variant_missing_raises(ae_product):
    product, _, _ = ae_product
    with pytest.raises(ValueError):
        _resolve_selected_variant(product, None)


def test_resolve_selected_variant_wrong_product_raises(db, ae_product):
    product, _, _ = ae_product
    other = Product(
        id=uuid.uuid4(),
        name="Outro",
        price=Decimal("10.00"),
        is_active=True,
    )
    db.session.add(other)
    db.session.flush()
    other_variant = ProductVariant(
        product_id=other.id,
        color="Cinza",
        size="P",
        stock=3,
        is_active=True,
        price=Decimal("5.00"),
    )
    db.session.add(other_variant)
    db.session.commit()
    with pytest.raises(ValueError):
        _resolve_selected_variant(product, str(other_variant.id))


# ── Carrinho / checkout variant-aware ────────────────────────────────────────

def test_two_variants_same_product_same_size_persist(db, client, make_user, ae_product):
    product, rosa_m, azul_m = ae_product
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})

    for variant in (rosa_m, azul_m):
        resp = client.post(
            f"/produtos/{product.id}/processar",
            data={
                "action": "add_to_cart",
                "variant_id": str(variant.id),
                "quantity": "1",
            },
            follow_redirects=True,
        )
        assert "adicionado ao carrinho" in resp.get_data(as_text=True).lower()

    cart = Cart.query.filter_by(user_id=user.id).first()
    items = list(cart.items)
    assert len(items) == 2
    variant_ids = {it.variant_id for it in items}
    assert variant_ids == {rosa_m.id, azul_m.id}
    for it in items:
        assert Decimal(str(it.unit_price)) == Decimal(str(product.price))
    # Ordem das variantes (Rosa/M, Azul/M) não importa; selected_size guarda
    # SOMENTE o tamanho (nunca cor+tamanho), evitando overflow em VARCHAR(10).
    assert {it.selected_size for it in items} == {"M"}


def test_buy_now_without_variant_redirects_with_warning(db, client, make_user, ae_product):
    product, _, _ = ae_product
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})

    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={
            "action": "buy_now",
            "quantity": "1",
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True).lower()
    assert "selecione uma variação" in body


def test_validate_cart_for_checkout_insufficient_variant_stock(
    db, make_user, ae_product
):
    user = make_user()
    product, rosa_m, _ = ae_product
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    cart.items.append(
        CartItem(
            id=uuid.uuid4(),
            product_id=product.id,
            selected_size="Rosa M",
            variant_id=rosa_m.id,
            quantity=99,
            unit_price=Decimal(str(product.price)),
        )
    )
    db.session.commit()

    with pytest.raises(ValueError) as exc_info:
        _validate_cart_for_checkout(cart)
    assert "excede o estoque" in str(exc_info.value)


def test_product_page_sanitized_description_and_variant_selector(
    client, ae_product
):
    product, rosa_m, azul_m = ae_product
    resp = client.get(f"/produtos/{product.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Seletor de variante presente
    assert 'name="variant_id"' in body
    assert "Rosa M" in body
    assert "Azul M" in body

    # Descrição sanitizada (script removido, sem handlers)
    assert '<script>alert("x")</script>' not in body
    assert "<script>alert" not in body

    # Nenhum SKU externo vaza no HTML
    assert rosa_m.supplier_sku_id not in body
    assert azul_m.supplier_sku_id not in body
    assert "supplier_sku_id" not in body
    assert "external_sku_id" not in body


# ── Reserva / liberação de estoque por variante ─────────────────────────────

def test_reserve_stock_variant_decrements_variant_and_product(db, ae_product):
    product, rosa_m, _ = ae_product

    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))

    assert rosa_m.stock == 2
    assert product.stock == 7


def test_reserve_stock_variant_insufficient_raises(db, ae_product):
    product, rosa_m, _ = ae_product
    # rosa_m.stock == 4; reservar 5 deve falhar
    with pytest.raises(ValueError) as exc_info:
        pr._reserve_stock(_variant_item(product, rosa_m, quantity=5))
    assert "insuficiente" in str(exc_info.value)

    assert rosa_m.stock == 4
    assert product.stock == 9


def test_release_stock_variant_restores(db, make_user, ae_product):
    user = make_user()
    product, rosa_m, _ = ae_product

    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2
    assert product.stock == 7

    order = _make_variant_order(db, user, product, rosa_m, quantity=2)
    pr._release_stock(order)

    assert rosa_m.stock == 4
    assert product.stock == 9
    assert order.stock_reserved is False


def test_approved_sync_no_double_decrement_variant(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)

    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    synced = pr._sync_payment("9010")
    assert synced.status == "paid"

    assert rosa_m.stock == 2
    assert synced.stock_reserved is True


def test_late_approved_refund_releases_reservation(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)

    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    pr._release_stock(order)
    assert order.stock_reserved is False
    assert rosa_m.stock == 4
    assert product.stock == 9

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )
    synced = pr._sync_payment("9012")
    assert synced.status == "paid"
    assert synced.stock_reserved is True
    assert rosa_m.stock == 2
    assert product.stock == 7

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "refunded", "refunded"),
    )
    synced = pr._sync_payment("9013")
    assert synced.status == "refunded"
    assert synced.stock_reserved is False
    assert rosa_m.stock == 4
    assert product.stock == 9


def test_second_refund_does_not_double_release(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)
    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))

    def refund_payload(pid):
        return _mp_payload(pid, order, "refunded", "refunded")

    monkeypatch.setattr(pr, "get_payment", refund_payload)
    first = pr._sync_payment("9014")
    assert first.status == "refunded"
    assert rosa_m.stock == 4

    second = pr._sync_payment("9015")
    assert second.status == "refunded"
    assert second.stock_reserved is False
    assert rosa_m.stock == 4
    assert product.stock == 9


def test_payment_failed_releases_variant_reservation(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)
    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2
    assert product.stock == 7

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(
            pid, order, "rejected", "rejected"
        ),
    )
    synced = pr._sync_payment("9016")
    assert synced.status == "payment_failed"
    assert synced.stock_reserved is False
    assert rosa_m.stock == 4
    assert product.stock == 9


def test_reserve_approved_keeps_no_double_debit(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)
    order.stock_reserved = True
    db.session.commit()
    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )
    synced = pr._sync_payment("9017")
    assert synced.status == "paid"
    assert synced.stock_reserved is True
    assert rosa_m.stock == 2


def test_release_expired_reservations_variant(
    db, make_user, ae_product
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)
    order.created_at = utcnow() - timedelta(hours=48)
    order.mp_preference_expires_at = utcnow() - timedelta(hours=24)
    db.session.commit()
    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2

    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    release_expired_stock_reservations(hours=24)

    assert rosa_m.stock == 4
    assert product.stock == 9
    assert order.stock_reserved is False


def test_refunded_sync_releases_variant_reservation(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, rosa_m, _ = ae_product
    order = _make_variant_order(db, user, product, rosa_m, quantity=2)

    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "refunded", "refunded"),
    )

    synced = pr._sync_payment("9011")
    assert synced.status == "refunded"

    assert rosa_m.stock == 4
    assert synced.stock_reserved is False


# ── Fase 7.2.1 ───────────────────────────────────────────────────────────────

# B: estoque zerado no AliExpress -> estado "sem variações disponíveis".

def _sized_product(db, variants):
    product = Product(
        id=uuid.uuid4(),
        name="Produto Seletores",
        description="<p>ok</p>",
        price=Decimal("100.00"),
        cost=Decimal("40.00"),
        stock=sum(int(v.stock or 0) for v in variants),
        is_active=True,
        is_draft=False,
        source="aliexpress",
        external_id=str(uuid.uuid4()),
        currency="BRL",
        target_margin_percent=Decimal("30.00"),
        pricing_status="priced",
    )
    db.session.add(product)
    db.session.flush()
    for v in variants:
        v.product_id = product.id
    db.session.add_all(variants)
    db.session.commit()
    db.session.refresh(product)
    return product


def test_no_in_stock_variant_shows_unavailable_state(db, client):
    out_of_stock = _sku("x-1", "Rosa", "M", 0)
    product = _sized_product(db, [out_of_stock])

    resp = client.get(f"/produtos/{product.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "Sem variações disponíveis no momento" in body
    # Não cai no fluxo interno por tamanho (P/M/G/GG): nenhum radio de tamanho
    # é renderizado (a string name="size" existe no JS, não no HTML).
    assert 'id="size-' not in body
    assert "Comprar Agora" in body


# C: selected_size = variant.size or "UNICO" (curto, dentro de VARCHAR(10)).

def test_cart_selected_size_uses_size_only_when_size_absent(
    db, client, make_user
):
    no_size = _sku("x-2", "Verde", None, 4)
    product = _sized_product(db, [no_size])
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})

    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={"action": "add_to_cart", "variant_id": str(no_size.id), "quantity": "1"},
        follow_redirects=True,
    )
    assert "adicionado ao carrinho" in resp.get_data(as_text=True).lower()

    cart = Cart.query.filter_by(user_id=user.id).first()
    item = list(cart.items)[0]
    assert item.selected_size == "UNICO"
    assert item.variant_id == no_size.id


def test_long_size_stored_complete_no_truncation(db, client, make_user):
    long_size = _sku("x-3", "Preto", "EXTRA-GRANDE-GG", 4)
    product = _sized_product(db, [long_size])
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})

    client.post(
        f"/produtos/{product.id}/processar",
        data={"action": "add_to_cart", "variant_id": str(long_size.id), "quantity": "1"},
        follow_redirects=True,
    )

    cart = Cart.query.filter_by(user_id=user.id).first()
    item = list(cart.items)[0]
    # Contrato novo (migração 0028): rótulo completo, sem truncamento
    # silencioso; identidade continua sendo o variant_id.
    assert item.selected_size == "EXTRA-GRANDE-GG"
    assert item.variant_id == long_size.id


# D: seletores reais Cor/Tamanho + fallback de atributos + "Opção N".

def test_page_renders_color_and_size_selectors(client, ae_product):
    product, rosa_m, azul_m = ae_product
    resp = client.get(f"/produtos/{product.id}")
    body = resp.get_data(as_text=True)

    assert 'data-dimension="color"' in body
    assert 'data-dimension="size"' in body
    assert "Rosa" in body
    assert "Azul" in body
    assert "Tamanho" in body
    # SKUs externos continuam ocultos
    assert rosa_m.supplier_sku_id not in body


def test_generic_option_labels_when_no_distinguishing_info(db):
    v1 = ProductVariant(
        id=uuid.uuid4(),
        color=None,
        size=None,
        stock=2,
        price=Decimal("5.00"),
        is_active=True,
    )
    v2 = ProductVariant(
        id=uuid.uuid4(),
        color=None,
        size=None,
        stock=2,
        price=Decimal("5.00"),
        is_active=True,
    )
    product = _sized_product(db, [v1, v2])

    selector = _build_variant_selector(product)
    assert selector["uses_variants"] is True
    assert len(selector["dimensions"]) == 1
    assert selector["dimensions"][0]["key"] == "option"
    assert selector["dimensions"][0]["options"] == ["Opção 1", "Opção 2"]
    assert {v["id"] for v in selector["variants"]} == {str(v1.id), str(v2.id)}
    # Nunca expõe SKUs externos
    for v in selector["variants"]:
        assert "supplier_sku_id" not in v
        assert "external_sku_id" not in v


def test_attribute_fallback_labels(db):
    v = ProductVariant(
        id=uuid.uuid4(),
        color=None,
        size=None,
        stock=3,
        price=Decimal("5.00"),
        is_active=True,
        attributes=[
            {"name": "Color", "value": "Vermelho"},
            {"name": "Size", "value": "L"},
        ],
    )
    product = _sized_product(db, [v])

    selector = _build_variant_selector(product)
    assert len(selector["dimensions"]) == 2
    assert selector["dimensions"][0]["options"] == ["Vermelho"]
    assert selector["dimensions"][1]["options"] == ["L"]
    assert selector["variants"][0]["dims"] == ["Vermelho", "L"]


# E: validação cruzada variante/produto e idempotência do refund.

def test_reserve_stock_rejects_variant_from_other_product(db, ae_product):
    product, _, _ = ae_product
    other = Product(
        id=uuid.uuid4(),
        name="Outro",
        price=Decimal("10.00"),
        is_active=True,
    )
    db.session.add(other)
    db.session.flush()
    other_variant = ProductVariant(
        product_id=other.id,
        color="Cinza",
        size="P",
        stock=3,
        is_active=True,
        price=Decimal("5.00"),
    )
    db.session.add(other_variant)
    db.session.flush()
    db.session.commit()

    items = [
        {
            "product_id": product.id,
            "product_name": product.name,
            "quantity": 1,
            "variant_id": other_variant.id,
        }
    ]
    with pytest.raises(ValueError) as exc_info:
        pr._reserve_stock(items)
    assert "não está mais disponível" in str(exc_info.value)


def test_validate_cart_rejects_variant_from_other_product(db, make_user, ae_product):
    from loja.modules.models import ProductVariant as PV

    user = make_user()
    product, _, _ = ae_product
    other = Product(
        id=uuid.uuid4(),
        name="Outro",
        price=Decimal("10.00"),
        is_active=True,
    )
    db.session.add(other)
    db.session.flush()
    foreign_variant = PV(
        product_id=other.id,
        color="Bege",
        size="M",
        stock=3,
        is_active=True,
        price=Decimal("5.00"),
    )
    db.session.add(foreign_variant)

    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    cart.items.append(
        CartItem(
            id=uuid.uuid4(),
            product_id=product.id,
            selected_size="M",
            variant_id=foreign_variant.id,
            quantity=1,
            unit_price=Decimal(str(product.price)),
        )
    )
    db.session.commit()

    with pytest.raises(ValueError) as exc_info:
        _validate_cart_for_checkout(cart)
    assert "indisponível" in str(exc_info.value)


def test_release_stock_restores_exactly_once(db, make_user, ae_product):
    user = make_user()
    product, rosa_m, _ = ae_product

    pr._reserve_stock(_variant_item(product, rosa_m, quantity=2))
    assert rosa_m.stock == 2

    order = _make_variant_order(db, user, product, rosa_m, quantity=2)
    pr._release_stock(order)
    assert rosa_m.stock == 4
    assert order.stock_reserved is False

    # Segunda chamada é idempotente: não devolve 2x.
    pr._release_stock(order)
    assert rosa_m.stock == 4
    assert product.stock == 9


# ── Fase 7.2.2 ───────────────────────────────────────────────────────────────

# F: pagamento aprovado tardio / expiração / lifecycle completo com refund.

def _foreign_variant(db, product_price="10.00"):
    other = Product(
        id=uuid.uuid4(),
        name="Outro Produto",
        price=Decimal(product_price),
        is_active=True,
    )
    db.session.add(other)
    db.session.flush()
    foreign = ProductVariant(
        id=uuid.uuid4(),
        product_id=other.id,
        color="Cinza",
        size="P",
        stock=7,
        is_active=True,
        price=Decimal("5.00"),
    )
    db.session.add(foreign)
    db.session.commit()
    return foreign


def test_sync_approved_cross_product_variant_manual_review(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, _, _ = ae_product
    foreign = _foreign_variant(db)
    order = _make_variant_order(db, user, product, foreign, quantity=2)
    # Simula uma reserva pré-existente (stock_reserved=True), sem registrar
    # o desconto aqui: deve seguir para manual_review sem mexer em estoque.
    order.stock_reserved = True
    db.session.commit()

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )
    synced = pr._sync_payment("9201")

    assert synced.status == "manual_review"
    assert synced.stock_reserved is True
    assert product.stock == 9
    assert foreign.stock == 7


def test_release_cross_product_variant_aborts_and_rolls_back(
    db, make_user, ae_product
):
    user = make_user()
    product, _, _ = ae_product
    foreign = _foreign_variant(db)
    order = _make_variant_order(db, user, product, foreign, quantity=2)
    order.stock_reserved = True
    db.session.commit()

    with pytest.raises(ValueError):
        pr._release_stock(order)

    # Rollback: nenhum estoque é alterado isoladamente e a reserva não fecha.
    db.session.rollback()
    db.session.expire_all()
    assert product.stock == 9
    assert foreign.stock == 7
    assert order.stock_reserved is True


# G: unicidade/validade das combinações de dimensões no seletor.

def test_selector_no_duplicate_combinations_standard(ae_product):
    product, _, _ = ae_product
    selector = _build_variant_selector(product)
    _assert_selector_invariant(selector)


def test_selector_duplicate_dimensions_disambiguated(db):
    a = _sku("a-1", "Rosa", "M", 3)
    b = _sku("a-2", "Rosa", "M", 3)
    product = _sized_product(db, [a, b])

    selector = _build_variant_selector(product)
    _assert_selector_invariant(selector)

    keys = [d["key"] for d in selector["dimensions"]]
    assert "color" in keys
    assert "size" in keys
    assert "option" in keys

    tuples = [tuple(v["dims"]) for v in selector["variants"]]
    assert len(tuples) == len(set(tuples)) == 2


def test_selector_partial_missing_dimension_valid(db):
    a = _sku("b-1", "Rosa", "M", 3)
    b = _sku("b-2", "Azul", None, 3)
    product = _sized_product(db, [a, b])

    selector = _build_variant_selector(product)
    _assert_selector_invariant(selector)

    size_idx = next(
        i for i, d in enumerate(selector["dimensions"]) if d["key"] == "size"
    )
    for variant in selector["variants"]:
        assert variant["dims"][size_idx] in selector["dimensions"][size_idx]["options"]

    tuples = [tuple(v["dims"]) for v in selector["variants"]]
    assert len(tuples) == len(set(tuples))


# H: carrinho/checkout real com variante e OrderItem.variant_id persistido.

def test_same_variant_added_twice_increments_single_item(
    db, client, make_user, ae_product
):
    product, rosa_m, _ = ae_product
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})

    for _ in range(2):
        resp = client.post(
            f"/produtos/{product.id}/processar",
            data={
                "action": "add_to_cart",
                "variant_id": str(rosa_m.id),
                "quantity": "1",
            },
            follow_redirects=True,
        )
        assert "adicionado ao carrinho" in resp.get_data(as_text=True).lower()

    cart = Cart.query.filter_by(user_id=user.id).first()
    items = list(cart.items)
    assert len(items) == 1
    assert items[0].variant_id == rosa_m.id
    assert items[0].quantity == 2


def test_anonymous_selects_variant_then_login_keeps_variant(
    db, client, make_user, ae_product
):
    product, rosa_m, _ = ae_product
    user = make_user()

    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={
            "action": "add_to_cart",
            "variant_id": str(rosa_m.id),
            "quantity": "1",
        },
    )
    assert resp.status_code == 302

    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
        follow_redirects=True,
    )

    cart = Cart.query.filter_by(user_id=user.id).first()
    assert cart is not None
    items = list(cart.items)
    assert len(items) == 1
    assert items[0].variant_id == rosa_m.id
    assert items[0].quantity == 1


def test_real_checkout_persists_variant_id_and_uses_product_price(
    db, client, make_user, ae_product, monkeypatch
):
    from loja.modules.getway_mercado_pago.payment_models import OrderItem
    from test_shipping_quote import _fresh
    from loja.services.supplier_freight_quotes import store_reference_freight_quote
    from loja.modules.models import SupplierProduct

    user = make_user()
    product, rosa_m, _ = ae_product
    # Preço da variante (custo/supplier) diverge do preço de venda do produto;
    # o vendido é sempre Product.price.
    rosa_m.price = Decimal("50.00")
    link = db.session.get(SupplierProduct, rosa_m.supplier_product_id)
    link.import_duty = Decimal("0.00")
    link.icms = Decimal("0.00")
    db.session.commit()

    from sqlalchemy import text
    # Use the same explicit test schema as the quote service tests.
    from test_shipping_quote import reference_quote_table
    table_fixture = reference_quote_table.__wrapped__(db)
    next(table_fixture)
    fresh = _fresh(link.id, cost="20.00")[str(link.id)]
    fresh['supplier_unit_cost'] = '46.60'
    store_reference_freight_quote(supplier_product_id=link.id, dest_cep='01310100',
        quote=fresh, provider_method='test', evidence_ref='synthetic-variant-cost')
    client.post("/login", data={"email": user.email, "password": "senha123"})

    monkeypatch.setattr(pr, "quote_checkout_shipping", lambda item_data, cep: Decimal("10.00"))
    monkeypatch.setattr(
        pr,
        "create_preference",
        lambda order: {
            "preference_id": "pref-9222",
            "checkout_url": "https://mp.example/x",
        },
    )
    from test_shipping_quote import preset_shipping_quote

    preset_shipping_quote(
        client, "01310100",
        [{"product_id": product.id, "variant_id": rosa_m.id, "quantity": 1}],
        "10.00",
    )

    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product",
            "product_id": str(product.id),
            "quantity": "1",
            "variant_id": str(rosa_m.id),
            "selected_size": "M",
            "nome": "Cliente Teste",
            "email": user.email,
            "telefone": "+55 (63) 99876-5432",
            "cep": "01310100",
            "estado": "SP",
            "endereco": "Rua Teste",
            "numero": "10",
            "bairro": "Centro",
            "cidade": "São Paulo",
        },
    )
    assert resp.status_code == 302

    order = Order.query.filter_by(user_id=user.id).first()
    assert order is not None
    item = OrderItem.query.filter_by(order_id=order.id).first()
    assert item.variant_id == rosa_m.id
    assert Decimal(str(item.unit_price)) == Decimal(str(product.price))
    # Estoque debitado na reserva de criação do pedido.
    assert rosa_m.stock == 3


def test_legacy_product_without_variant_uses_old_flow(db, client, make_user):
    product = Product(
        id=uuid.uuid4(),
        name="Legado",
        description="<p>ok</p>",
        price=Decimal("50.00"),
        cost=Decimal("20.00"),
        stock=5,
        is_active=True,
        is_draft=False,
        source="internal",
    )
    db.session.add(product)
    db.session.commit()
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})

    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={"action": "add_to_cart", "size": "M", "quantity": "1"},
        follow_redirects=True,
    )
    assert "adicionado ao carrinho" in resp.get_data(as_text=True).lower()

    cart = Cart.query.filter_by(user_id=user.id).first()
    item = list(cart.items)[0]
    assert item.variant_id is None
    assert item.selected_size == "M"


# ── Fase 7.2.5 ────────────────────────────────────────────────────────────────
# 2) AliExpress nunca vira fluxo legacy por falta de variant_id no checkout.
# 3) _reserve_stock bloqueia AliExpress sem variant_id e não mexe em estoque.
# 4) _sync_payment: AliExpress variantless -> manual_review, sem débito em
#    Product.stock; legacy continua normal; retry idempotente.


def _add_variantless_aliexpress_item(db, user, product):
    """Cria um carrinho com um CartItem AliExpress sem variant_id.

    `variant_id` fica None simulando exatamente a remoção da variante via
    ON DELETE SET NULL (a FK vira NULL mas o item continua no carrinho).
    """
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    cart.items.append(
        CartItem(
            id=uuid.uuid4(),
            product_id=product.id,
            selected_size="M",
            variant_id=None,
            quantity=1,
            unit_price=Decimal(str(product.price)),
        )
    )
    db.session.commit()
    return cart


def test_validate_cart_aliexpress_variant_id_none_is_rejected(
    db, make_user, ae_product
):
    user = make_user()
    product, _, _ = ae_product
    assert product.stock > 0

    cart = _add_variantless_aliexpress_item(db, user, product)

    # Mesmo com Product.stock > 0, um AliExpress sem variant_id NÃO cai para
    # o fluxo legacy por selected_size.
    with pytest.raises(ValueError) as exc_info:
        _validate_cart_for_checkout(cart)
    assert "variação" in str(exc_info.value).lower()


def test_validate_cart_aliexpress_variant_removed_set_null_not_legacy(
    db, make_user, ae_product
):
    """Variante removida (FK SET NULL) não deixa o carrinho cair para legacy."""
    user = make_user()
    product, rosa_m, _ = ae_product

    # Monta o item COM variante e depois simula o FK ON DELETE SET NULL:
    # a variante "removida" vira NULL, mas o item permanece.
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    cart.items.append(
        CartItem(
            id=uuid.uuid4(),
            product_id=product.id,
            selected_size="M",
            variant_id=rosa_m.id,
            quantity=1,
            unit_price=Decimal(str(product.price)),
        )
    )
    db.session.commit()

    # FK SET NULL (mesmo efeito de apagar a ProductVariant da base).
    item = list(cart.items)[0]
    item.variant_id = None
    db.session.commit()

    with pytest.raises(ValueError) as exc_info:
        _validate_cart_for_checkout(cart)
    # NÃO deve acusar erro de "tamanho inválido" (fluxo legacy); deve exigir
    # a variação.
    assert "variação" in str(exc_info.value).lower()


def test_reserve_stock_aliexpress_without_variant_blocks_no_stock_change(
    db, ae_product
):
    product, rosa_m, azul_m = ae_product
    initial_product_stock = product.stock
    initial_rosa = rosa_m.stock
    initial_azul = azul_m.stock

    item = [
        {
            "product_id": product.id,
            "product_name": product.name,
            "selected_size": "M",
            "quantity": 2,
            "unit_price": Decimal(str(product.price)),
            "line_total": Decimal(str(product.price)) * 2,
        }
    ]

    with pytest.raises(ValueError) as exc_info:
        pr._reserve_stock(item)
    assert "variação" in str(exc_info.value).lower()

    # Nenhum estoque pode ter sido debitado (nem agregado, nem por variante).
    db.session.refresh(product)
    db.session.refresh(rosa_m)
    db.session.refresh(azul_m)
    assert product.stock == initial_product_stock
    assert rosa_m.stock == initial_rosa
    assert azul_m.stock == initial_azul


def _make_variantless_order(db, user, product, quantity=2, stock_reserved=True):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

    unit_price = Decimal(str(product.price))
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=unit_price * quantity,
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=unit_price * quantity + Decimal("10.00"),
        stock_reserved=stock_reserved,
        customer_name="Cliente Teste",
        customer_email=user.email,
        zip_code="01310100",
        street="Rua Teste",
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
            variant_id=None,
            quantity=quantity,
            unit_price=unit_price,
            line_total=unit_price * quantity,
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_sync_payment_aliexpress_variantless_late_approved_manual_review(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, _, _ = ae_product
    order = _make_variantless_order(db, user, product, stock_reserved=True)

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    synced = pr._sync_payment("sync-a")
    assert synced.status == "manual_review"
    assert "Variação não informada" in str(synced.manual_review_reason)

    # Nenhum débito extra em Product.stock.
    db.session.refresh(product)
    assert product.stock == 9


def test_sync_payment_aliexpress_variantless_unreserved_manual_review_no_debit(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, _, _ = ae_product
    # stock_reserved=False: sem reserva prévia; mesmo assim não pode debitar
    # apenas Product.stock para um item AliExpress sem variante.
    order = _make_variantless_order(db, user, product, stock_reserved=False)

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    synced = pr._sync_payment("sync-b")
    assert synced.status == "manual_review"
    assert "Variação não informada" in str(synced.manual_review_reason)

    db.session.refresh(product)
    assert product.stock == 9  # intocado, sem segundo débito


def test_sync_payment_legacy_variantless_continues_normal(
    db, make_user, make_product, monkeypatch
):
    user = make_user()
    product = make_product(stock=10)
    order = _make_variantless_order(db, user, product, stock_reserved=False)

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    synced = pr._sync_payment("sync-c")
    assert synced.status == "paid"
    db.session.refresh(product)
    assert product.stock == 8  # débito legado normal em Product.stock


def test_sync_payment_retry_after_manual_review_is_idempotent(
    db, make_user, ae_product, monkeypatch
):
    user = make_user()
    product, _, _ = ae_product
    order = _make_variantless_order(db, user, product, stock_reserved=True)

    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _mp_payload(pid, order, "approved", "accredited"),
    )

    first = pr._sync_payment("sync-d")
    assert first.status == "manual_review"

    # Retry: mesma aprovação não deve reprocessar nem alterar o estado.
    retry = pr._sync_payment("sync-d")
    assert retry.status == "manual_review"
    db.session.refresh(product)
    assert product.stock == 9

