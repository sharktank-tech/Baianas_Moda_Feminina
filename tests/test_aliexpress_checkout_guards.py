"""Bloqueios canônicos de compra no fluxo REAL de checkout (revisão PR #123).

Dois requisitos validados de ponta a ponta (validação do carrinho + criação
do pedido), sem mockar nenhum controle canônico:

1. Divergência de custo/moeda do fornecedor invalida a aprovação
   operacional e impede NOVA compra.
2. Origem nacional é validada POR VARIANTE no checkout — inclusive quando a
   confirmação é removida DEPOIS da publicação.

Frete/aquisição são aparados (fora do escopo destes bloqueios); o caminho
real de carrinho, reserva de estoque e criação do pedido roda intacto.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from test_checkout_idempotency import (
    _cart_form,
    _freeze_endpoint_time,
    _login,
    _patch_shipping,
    _recording_preference,
)

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import (
    Cart,
    CartItem,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)
from loja.services.shipping_origin import (
    clear_manual_confirmation,
    record_manual_confirmation,
)


def _ae_published(db, *, stock=5):
    """Produto AliExpress PUBLICADO com uma variante vendável."""
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp Checkout",
        channel_type="aliexpress",
        seller_id="SELLER_CK",
    )
    db.session.add(supplier)
    db.session.flush()

    product = Product(
        id=uuid.uuid4(),
        name="Vestido AE",
        description="Descrição do vestido.",
        image1="https://exemplo.com/v.png",
        category="feminino",
        price=Decimal("142.86"),
        target_margin_percent=Decimal("30"),
        currency="BRL",
        source="aliexpress",
        external_id="EXT_CK",
        is_draft=False,
        is_active=True,
        pricing_status="priced",
        stock=stock,
    )
    db.session.add(product)
    db.session.flush()

    link = SupplierProduct(
        id=uuid.uuid4(),
        supplier_id=supplier.id,
        product_id=product.id,
        supplier_product_id="SP_CK",
        cost=Decimal("80.00"),
        currency="BRL",
        freight_status="available",
        shipping_cost=Decimal("4.00"),
        freight_currency="BRL",
        landed_cost=Decimal("84.00"),
        import_duty=Decimal("0.00"),
        icms=Decimal("0.00"),
        is_active=True,
    )
    db.session.add(link)
    db.session.flush()

    variant = ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id=link.id,
        external_sku_id="EXT_CK-1",
        supplier_sku_id="EXT_CK-1",
        color="Vermelho",
        size="M",
        stock=stock,
        is_active=True,
    )
    db.session.add(variant)
    db.session.commit()
    return product, variant, link


def _make_cart(db, user, product, variant, quantity=1):
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(
        CartItem(
            id=uuid.uuid4(),
            cart_id=cart.id,
            product_id=product.id,
            variant_id=variant.id,
            selected_size="M",
            quantity=quantity,
            unit_price=product.price,
        )
    )
    db.session.commit()
    return cart


def _post_checkout(client, user, monkeypatch, *, follow=True):
    """POST real do checkout (frete/apuração aparados fora do escopo)."""
    import loja.services.acquisition_costs as acq
    import loja.views.payments_routes as pr

    pr_module = _patch_shipping(monkeypatch)
    _freeze_endpoint_time(monkeypatch, pr_module)
    monkeypatch.setattr(
        pr_module, "create_preference", _recording_preference([])
    )
    monkeypatch.setattr(
        acq, "freeze_checkout_acquisition", lambda *args, **kwargs: None
    )
    assert pr_module is pr
    form = _cart_form(user)
    form["telefone"] = "11999998888"
    return client.post(
        "/pagamentos/mercado-pago",
        data=form,
        follow_redirects=follow,
    )


def test_divergencia_de_custo_bloqueia_checkout_inteiro(
    client, db, make_user, monkeypatch
):
    """Divergência de custo sinalizada: nem abre checkout, nem cria pedido."""
    user = make_user()
    product, variant, link = _ae_published(db)
    link.sync_error = "supplier_cost_changed"
    product.sync_error = "supplier_cost_changed"
    db.session.commit()

    orders_before = Order.query.count()
    _login(client, user)
    _make_cart(db, user, product, variant)

    # 1) Abertura do checkout: validação do carrinho recusa com mensagem.
    resp = client.get("/checkout/carrinho", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "divergência de custo" in body
    assert Order.query.count() == orders_before

    # 2) Criação do pedido: reserva de estoque também recusa.
    resp = _post_checkout(client, user, monkeypatch)
    body = resp.get_data(as_text=True)
    assert "divergência de custo" in body
    assert Order.query.count() == orders_before, "pedido criado apesar do bloqueio"

    # Estoque intacto: nada foi debitado.
    db.session.expire_all()
    refreshed = db.session.get(ProductVariant, variant.id)
    assert refreshed.stock == 5


def test_divergencia_de_moeda_bloqueia_checkout_inteiro(
    client, db, make_user, monkeypatch
):
    user = make_user()
    product, variant, link = _ae_published(db)
    link.sync_error = "currency_changed"
    db.session.commit()

    orders_before = Order.query.count()
    _login(client, user)
    _make_cart(db, user, product, variant)

    resp = _post_checkout(client, user, monkeypatch)
    assert "divergência de moeda" in resp.get_data(as_text=True)
    assert Order.query.count() == orders_before


def test_variante_nacional_sem_origem_bloqueia_checkout(
    client, db, make_user, monkeypatch
):
    """Operação nacional selecionada sem origem comprovada: bloqueia."""
    user = make_user()
    product, variant, link = _ae_published(db)
    variant.national_shipping_operation = True
    db.session.commit()

    orders_before = Order.query.count()
    _login(client, user)
    _make_cart(db, user, product, variant)

    resp = client.get("/checkout/carrinho", follow_redirects=True)
    assert "origem de envio nacional" in resp.get_data(as_text=True)

    resp = _post_checkout(client, user, monkeypatch)
    assert "origem de envio nacional" in resp.get_data(as_text=True)
    assert Order.query.count() == orders_before


def test_confirmacao_removida_apos_publicacao_bloqueia_checkout(
    client, db, make_user, monkeypatch
):
    """Confirmado e publicado, depois removido: a compra nova trava."""
    user = make_user()
    product, variant, link = _ae_published(db)
    variant.national_shipping_operation = True
    db.session.commit()

    # Confirmado ANTES (produto já publicado) e depois removido.
    record_manual_confirmation(
        supplier_product_id=link.id,
        origin="brazil",
        evidence_ref="ORIGEM-CK-001",
        sku_id=variant.supplier_sku_id,
        user_id=user.id,
    )
    db.session.commit()
    assert clear_manual_confirmation(
        supplier_product_id=link.id,
        sku_id=variant.supplier_sku_id,
    )
    db.session.commit()

    orders_before = Order.query.count()
    _login(client, user)
    _make_cart(db, user, product, variant)

    resp = client.get("/checkout/carrinho", follow_redirects=True)
    assert "origem de envio nacional" in resp.get_data(as_text=True)

    resp = _post_checkout(client, user, monkeypatch)
    assert "origem de envio nacional" in resp.get_data(as_text=True)
    assert Order.query.count() == orders_before


def test_variante_nacional_com_origem_comprovada_passa_no_checkout(
    client, db, make_user, monkeypatch
):
    """Controle: origem BR comprovada na variante comprada -> pedido sai."""
    user = make_user()
    product, variant, link = _ae_published(db)
    variant.national_shipping_operation = True
    db.session.commit()
    orders_before = Order.query.count()

    record_manual_confirmation(
        supplier_product_id=link.id,
        origin="brazil",
        evidence_ref="ORIGEM-CK-002",
        sku_id=variant.supplier_sku_id,
        user_id=user.id,
    )
    db.session.commit()

    _login(client, user)
    _make_cart(db, user, product, variant)

    resp = client.get("/checkout/carrinho")
    assert resp.status_code == 200

    resp = _post_checkout(client, user, monkeypatch, follow=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://mp.example/checkout/")
    assert Order.query.count() == orders_before + 1
    order = Order.query.filter_by(user_id=user.id).one()
    assert order.stock_reserved is True
    db.session.expire_all()
    assert db.session.get(ProductVariant, variant.id).stock == 4


def test_catalogo_internacional_sem_selecao_nao_bloqueia(
    client, db, make_user, monkeypatch
):
    """Controle: variante fora da operação nacional segue vendável."""
    user = make_user()
    product, variant, link = _ae_published(db)
    assert variant.national_shipping_operation is False
    orders_before = Order.query.count()

    _login(client, user)
    _make_cart(db, user, product, variant)

    resp = client.get("/checkout/carrinho")
    assert resp.status_code == 200

    resp = _post_checkout(client, user, monkeypatch, follow=False)
    assert resp.status_code == 302
    assert Order.query.count() == orders_before + 1
