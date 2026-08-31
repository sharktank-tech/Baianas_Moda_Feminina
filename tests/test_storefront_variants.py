"""Testes do storefront multivariante AliExpress (Fase 7.2).

Cobrimos: opção canônica (`variant_id`) no carrinho/checkout/pedido, preço de
venda sempre `Product.price`, estoque por variante na reserva/liberação, a não
exposição de supplier_sku_id/supplier_product_id, e a renderização sanitizada da
descrição no storefront.
"""

import uuid
from decimal import Decimal

import pytest

import loja.views.payments_routes as pr
from loja.modules.models import (
    Cart,
    CartItem,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)
from loja.views.routes import (
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
        shipping=Decimal("18.90"),
        discount=Decimal("0.00"),
        total=unit_price * quantity + Decimal("18.90"),
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
    assert {it.selected_size for it in items} == {"Rosa M", "Azul M"}


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
