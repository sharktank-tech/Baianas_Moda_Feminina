"""Compra manual no fornecedor + despacho (registro, sem automação).

Salvar nunca compra no AliExpress, consulta o fornecedor ou habilita
automação: só insere compra/itens/remessa/cobertura/auditoria, sem tocar
pagamento, preço, estoque ou reservas.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem, Payment
from loja.modules.models import (
    ManualOperationalAudit,
    ManualSupplierPurchase,
    Shipment,
    ShipmentItem,
)
from loja.services import manual_dispatch as dispatch

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _clean_manual_dispatch_data(db):
    """Isolamento: apaga compras/remessas/auditoria criadas em cada teste."""
    from loja.modules.models import Shipment as ShipmentModel
    from loja.modules.models import TrackingEvent as TrackingEventModel
    from loja.modules.models import (
        ManualSupplierPurchaseItem as PurchaseItemModel,
    )

    yield
    for model in (ShipmentItem, TrackingEventModel, ShipmentModel,
                  PurchaseItemModel, ManualSupplierPurchase,
                  ManualOperationalAudit):
        model.query.delete()
    db.session.commit()


def _product_item(db, user, product, *, quantity=2):
    order = Order(
        id=uuid.uuid4(), user_id=user.id, checkout_type="product",
        status="pending_payment", subtotal=Decimal("100.00"),
        shipping=Decimal("10.00"), discount=Decimal("0.00"),
        total=Decimal("110.00"), customer_name="Cliente Teste",
        customer_email="cliente@teste.com", zip_code="77066520",
        street="Rua Teste", number="10", neighborhood="Centro",
        city="Palmas", state="TO",
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        id=uuid.uuid4(), order_id=order.id, product_id=product.id,
        product_name=product.name, quantity=quantity,
        unit_price=product.price, line_total=product.price * quantity,
    )
    db.session.add(item)
    db.session.commit()
    return order, item


def _approve(db, order):
    order.status = "paid"
    order.mp_payment_id = uuid.uuid4().hex
    order.mp_status = "approved"
    order.paid_at = NOW
    order.payments.append(Payment(
        id=uuid.uuid4(), provider="mercado_pago",
        provider_payment_id=order.mp_payment_id, status="approved",
        amount=order.total, currency="BRL", raw_payload={},
    ))
    db.session.commit()
    return order


def _paid_order(db, user, product, *, quantity=2):
    order, item = _product_item(db, user, product, quantity=quantity)
    return _approve(db, order), item


def _purchase_args(order, item, **over):
    args = dict(
        order_id=order.id, external_order_id="ALI-1001",
        items={item.id: item.quantity}, purchased_at="2026-10-08T10:00:00",
        purchased_tz="America/Sao_Paulo", evidence_ref="EV-001",
    )
    args.update(over)
    return args


# --------------------------------------------------------------------------
# 1) Registro válido de compra e rastreio
# --------------------------------------------------------------------------

def test_registro_valido_compra_e_rastreio(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    stock_before = product.stock

    result = dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    assert result.created is True
    purchase = db.session.get(ManualSupplierPurchase, result.purchase_id)
    assert purchase.external_order_id == "ALI-1001"
    assert purchase.evidence_ref == "EV-001"

    res = dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=purchase.id,
        tracking_code="BR123456789XX", carrier="Correios",
        evidence_ref="EV-002", items={item.id: 1},
    )
    db.session.commit()
    assert res.created is True
    shipment = db.session.get(Shipment, res.shipment_id)
    assert shipment.status == "aguardando_postagem"
    assert shipment.carrier == "Correios"
    assert shipment.manual_purchase_id == purchase.id

    # Nada financeiro/operacional muda: preço, estoque, reservas, pedido.
    assert product.stock == stock_before
    assert order.total == Decimal("110.00") and order.status == "paid"


# --------------------------------------------------------------------------
# 2) Elegibilidade: pagamento e estados
# --------------------------------------------------------------------------

def test_pedido_sem_pagamento_bloqueado(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    # Sem prova de pagamento (paid sem evidência) e pendente: ambos recusam.
    order, item = _product_item(db, user, product)
    order.status = "paid"
    db.session.commit()
    with pytest.raises(ValueError, match="payment_not_confirmed"):
        dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.rollback()
    order.status = "pending_payment"
    db.session.commit()
    with pytest.raises(ValueError, match="order_status_not_eligible"):
        dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.rollback()


@pytest.mark.parametrize("status", [
    "cancelled", "refunded", "charged_back", "manual_review",
    "pending_payment", "payment_failed", "expired",
])
def test_estados_impedidos_bloqueados(db, make_user, make_product, status):
    user = make_user()
    product = make_product(stock=10)
    order, item = _product_item(db, user, product)
    order.user_id = user.id
    order.status = status
    order.mp_payment_id = uuid.uuid4().hex
    order.mp_status = "approved"
    order.paid_at = NOW
    db.session.commit()
    with pytest.raises(ValueError, match="order_status_not_eligible"):
        dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.rollback()


# --------------------------------------------------------------------------
# 3) Itens, quantidades e campos
# --------------------------------------------------------------------------

def test_item_outro_pedido_quantidade_campos(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    other, other_item = _paid_order(db, make_user(), product)
    base = _purchase_args(order, item)

    with pytest.raises(ValueError, match="order_item_not_in_order"):
        dispatch.register_manual_purchase(
            **{**base, "items": {other_item.id: 1}})
    db.session.rollback()
    with pytest.raises(ValueError, match="quantity_exceeds_sold"):
        dispatch.register_manual_purchase(
            **{**base, "items": {item.id: item.quantity + 1}})
    db.session.rollback()
    with pytest.raises(ValueError, match="quantity_invalid"):
        dispatch.register_manual_purchase(**{**base, "items": {item.id: 0}})
    db.session.rollback()
    for field, bad in [
        ("external_order_id", "  "), ("evidence_ref", ""),
        ("purchased_at", "ontem"), ("purchased_tz", "Lua/Olimpo"),
    ]:
        with pytest.raises(ValueError):
            dispatch.register_manual_purchase(**{**base, field: bad})
        db.session.rollback()
    assert ManualSupplierPurchase.query.count() == 0


# --------------------------------------------------------------------------
# 4) Compra dividida, despacho parcial, múltiplas remessas
# --------------------------------------------------------------------------

def test_compra_dividida_e_multiplas_remessas(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=20)
    order, item = _paid_order(db, user, product, quantity=4)

    def buy(code, qty):
        res = dispatch.register_manual_purchase(**_purchase_args(
            order, item, external_order_id=code, items={item.id: qty}))
        db.session.commit()
        return db.session.get(ManualSupplierPurchase, res.purchase_id)

    def ship(purchase, code, qty):
        res = dispatch.register_manual_shipment(
            order_id=order.id, purchase_id=purchase.id, tracking_code=code,
            carrier="Correios", evidence_ref="EV", items={item.id: qty})
        db.session.commit()
        return res

    pa = buy("ALI-A", 2)
    pb = buy("ALI-B", 2)
    assert ship(pa, "BR000000001XX", 1).created is True
    assert ship(pa, "BR000000002XX", 1).created is True
    assert ship(pb, "BR000000003XX", 2).created is True
    # Acima do vinculado à compra A (2): recusa.
    with pytest.raises(ValueError, match="quantity_exceeds_purchase"):
        dispatch.register_manual_shipment(
            order_id=order.id, purchase_id=pa.id, tracking_code="BR000000004XX",
            carrier="Correios", evidence_ref="EV", items={item.id: 1})
    db.session.rollback()
    assert Shipment.query.count() == 3


# --------------------------------------------------------------------------
# 5) Idempotência: reenvio e duplicados entre pedidos
# --------------------------------------------------------------------------

def test_reenvio_retorna_existente_sem_duplicar(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    first = dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    again = dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    assert again.created is False and again.purchase_id == first.purchase_id
    assert ManualSupplierPurchase.query.count() == 1

    s1 = dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=first.purchase_id,
        tracking_code="BR123456789XX", carrier="Correios",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()
    s2 = dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=first.purchase_id,
        tracking_code="br123456789xx", carrier="Outra",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()
    assert s2.created is False and s2.shipment_id == s1.shipment_id
    assert Shipment.query.count() == 1


def test_mesmo_numero_ou_rastreio_em_outro_pedido(db, make_user, make_product):
    user = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    other, other_item = _paid_order(db, make_user(), product)
    dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    with pytest.raises(ValueError, match="external_order_id_in_use"):
        dispatch.register_manual_purchase(
            **_purchase_args(other, other_item))
    db.session.rollback()


# --------------------------------------------------------------------------
# 6) Correções com justificativa, histórico e stale check
# --------------------------------------------------------------------------

def test_correcao_com_auditoria_e_stale_check(db, make_user, make_product):
    user = make_user()
    admin = make_user(is_admin=True)
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    res = dispatch.register_manual_purchase(
        **_purchase_args(order, item, admin_user_id=admin.id))
    db.session.commit()
    purchase = db.session.get(ManualSupplierPurchase, res.purchase_id)
    stamp = purchase.updated_at.isoformat()

    fixed = dispatch.correct_manual_purchase(
        purchase_id=purchase.id, external_order_id="ALI-2002",
        reason="digitado errado", admin_user_id=admin.id,
        expected_updated_at=stamp)
    db.session.commit()
    assert fixed.external_order_id == "ALI-2002"
    audit = ManualOperationalAudit.query.filter_by(
        entity="purchase", entity_id=str(purchase.id),
        field="external_order_id").all()
    assert len(audit) == 1
    assert (audit[0].field, audit[0].old_value, audit[0].new_value) == (
        "external_order_id", "ALI-1001", "ALI-2002")
    assert audit[0].admin_user_id == admin.id and audit[0].reason == "digitado errado"

    with pytest.raises(ValueError, match="stale_state_reload"):
        dispatch.correct_manual_purchase(
            purchase_id=purchase.id, external_order_id="ALI-3003",
            reason="x", admin_user_id=admin.id,
            expected_updated_at="2000-01-01T00:00:00")
    db.session.rollback()
    with pytest.raises(ValueError, match="reason_invalid"):
        dispatch.correct_manual_purchase(
            purchase_id=purchase.id, external_order_id="ALI-3003",
            reason=" ", admin_user_id=admin.id,
            expected_updated_at=db.session.get(
                ManualSupplierPurchase, purchase.id).updated_at.isoformat())
    db.session.rollback()
    with pytest.raises(ValueError, match="expected_updated_at_required"):
        dispatch.correct_manual_purchase(
            purchase_id=purchase.id, external_order_id="ALI-3003",
            reason="x", admin_user_id=admin.id)
    db.session.rollback()
    with pytest.raises(ValueError, match="expected_updated_at_invalid"):
        dispatch.correct_manual_purchase(
            purchase_id=purchase.id, external_order_id="ALI-3003",
            reason="x", admin_user_id=admin.id,
            expected_updated_at="nao-e-data")
    db.session.rollback()
    assert db.session.get(
        ManualSupplierPurchase, purchase.id).external_order_id == "ALI-2002"


def test_correcao_rastreio(db, make_user, make_product):
    user = make_user()
    admin = make_user(is_admin=True)
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    res = dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    ship = dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=res.purchase_id,
        tracking_code="BR123456789XX", carrier="Correios",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()
    shipment = db.session.get(Shipment, ship.shipment_id)
    stamp = shipment.updated_at.isoformat()

    dispatch.correct_manual_shipment(
        shipment_id=shipment.id, carrier="Jadlog", reason="etiqueta trocada",
        admin_user_id=admin.id, expected_updated_at=stamp)
    db.session.commit()
    assert db.session.get(Shipment, shipment.id).carrier == "Jadlog"
    rows = ManualOperationalAudit.query.filter_by(
        entity="shipment", entity_id=str(shipment.id),
        field="carrier").all()
    assert [(r.field, r.old_value, r.new_value) for r in rows] == [
        ("carrier", "Correios", "Jadlog")]
    assert order.status == "paid"  # correção não muda o pedido


# --------------------------------------------------------------------------
# 7) Rotas: detalhe com seções, registro via POST, permissões e CSRF
# --------------------------------------------------------------------------

def _paid_order_with_items(db, user, product):
    return _paid_order(db, user, product, quantity=2)


def test_detalhe_tem_tres_secoes_e_form_condicional(
    client, db, admin_user, login, make_product
):
    user = admin_user
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    login(user.email)
    html = client.get(f"/admin/pedidos/{order.id}").get_data(as_text=True)
    assert "Compra no fornecedor" in html
    assert "Despacho e rastreio" in html
    assert "Histórico operacional" in html
    assert "Registrar compra no AliExpress" in html
    # Endereço do operador visível na área administrativa.
    assert "77066520" in html


def test_form_oculto_sem_pagamento(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product(stock=10)
    order, item = _product_item(db, user, product)
    admin = make_user(is_admin=True)
    login(admin.email)
    html = client.get(f"/admin/pedidos/{order.id}").get_data(as_text=True)
    assert "Registrar compra no AliExpress" not in html


def test_registro_via_rota_e_flash(client, db, admin_user, login, make_product):
    product = make_product(stock=10)
    order, item = _paid_order(db, admin_user, product)
    login(admin_user.email)
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={
            "external_order_id": "ALI-9001",
            f"item_{item.id}": "2",
            "purchased_at": "2026-10-08T10:00",
            "purchased_tz": "America/Sao_Paulo",
            "evidence_ref": "EV-9001",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Compra registrada" in html
    assert "ALI-9001" in html
    assert ManualSupplierPurchase.query.count() == 1


def test_rota_recusa_sem_pagamento(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product(stock=10)
    order, item = _product_item(db, user, product)
    admin = make_user(is_admin=True)
    login(admin.email)
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={"external_order_id": "ALI-X", f"item_{item.id}": "1",
              "purchased_at": "2026-10-08T10:00", "evidence_ref": "EV"},
        follow_redirects=True,
    )
    assert "Pedido inelegível" in resp.get_data(as_text=True)
    assert ManualSupplierPurchase.query.count() == 0


def test_permissoes_e_metodos(client, db, make_user, make_product, login):
    user = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    paths = [
        f"/admin/pedidos/{order.id}/compra-manual",
    ]
    for path in paths:
        assert client.get(path).status_code == 405  # POST-only, sem login
        assert client.post(path).status_code == 302  # anônimo -> login
    login(make_user(is_admin=False).email)
    for path in paths:
        assert client.get(path).status_code == 405
        assert client.post(path).status_code == 302  # sem permissão


def test_csrf_bloqueia_sem_token(client, db, app, admin_user, login, make_product, monkeypatch):
    product = make_product(stock=10)
    order, item = _paid_order(db, admin_user, product)
    login(admin_user.email)
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={"external_order_id": "ALI-X"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert ManualSupplierPurchase.query.count() == 0


# --------------------------------------------------------------------------
# 8) Visão do cliente: isolamento e estados públicos
# --------------------------------------------------------------------------

def test_cliente_ve_somente_seus_dados(client, db, make_user, make_product, login):
    owner = make_user()
    other = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, owner, product)
    res = dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=res.purchase_id,
        tracking_code="BR123456789XX", carrier="Correios",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()

    login(other.email)
    assert client.get(f"/encomendas/pedido/{order.id}").status_code == 404
    login(owner.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert "BR123456789XX" in html
    assert "EV-001" not in html and "ALI-1001" not in html  # nada operacional
    assert "itens pendentes" in html  # 2 vendidos, 1 coberto


def test_cliente_sem_rastreio_estado_neutro(client, db, make_user, make_product, login):
    owner = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, owner, product)
    login(owner.email)
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert "rastreio" in html.lower() or "envio" in html.lower()


# --------------------------------------------------------------------------
# 9) Sem chamadas externas, sem mutação financeira
# --------------------------------------------------------------------------

def test_sem_chamadas_externas_sem_mutacao(db, make_user, make_product, monkeypatch):
    import loja.services.manual_dispatch as md

    def _boom(*args, **kwargs):
        raise AssertionError("chamada externa proibida")

    monkeypatch.setattr("loja.views.aliexpress_admin_routes.AliExpressClient", _boom, raising=False)
    user = make_user()
    admin = make_user(is_admin=True)
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product)
    snapshot = (product.stock, order.total, order.status, order.stock_reserved)
    res = dispatch.register_manual_purchase(
        **_purchase_args(order, item, admin_user_id=admin.id))
    db.session.commit()
    dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=res.purchase_id,
        tracking_code="BR123456789XX", carrier="Correios",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()
    ship_row = db.session.query(Shipment).first()
    dispatch.correct_manual_shipment(
        shipment_id=ship_row.id,
        carrier="Jadlog", reason="ajuste", admin_user_id=admin.id,
        expected_updated_at=ship_row.updated_at.isoformat())
    db.session.commit()
    db.session.expire_all()
    assert (product.stock, order.total, order.status, order.stock_reserved) == snapshot
    assert product.price == Decimal("49.90")


# --------------------------------------------------------------------------
# 10) Itens da revisão: parciais, vínculo, evidência privada, bucket
# --------------------------------------------------------------------------

def test_form_parcial_ignora_zeros_mas_exige_positiva(
    client, db, admin_user, login, make_product
):
    product = make_product(stock=10)
    order, item = _paid_order(db, admin_user, product)
    login(admin_user.email)
    # Zero em outro item + positiva neste: registra parcial.
    other, other_item = _paid_order(db, admin_user, product)
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={"external_order_id": "ALI-P", f"item_{item.id}": "1",
              f"item_{other_item.id}": "0",
              "purchased_at": "2026-10-08T10:00", "evidence_ref": "EV"},
        follow_redirects=True,
    )
    assert "Compra registrada" in resp.get_data(as_text=True)
    # Item de outro pedido mesmo com zero? não — outro pedido é recusado.
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={"external_order_id": "ALI-Q", f"item_{other_item.id}": "2",
              "purchased_at": "2026-10-08T10:00", "evidence_ref": "EV"},
        follow_redirects=True,
    )
    assert "não pertence" in resp.get_data(as_text=True)
    # Só zeros: items_required.
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={"external_order_id": "ALI-R", f"item_{item.id}": "0",
              "purchased_at": "2026-10-08T10:00", "evidence_ref": "EV"},
        follow_redirects=True,
    )
    assert "ao menos um item" in resp.get_data(as_text=True)
    # Negativo: recusado.
    resp = client.post(
        f"/admin/pedidos/{order.id}/compra-manual",
        data={"external_order_id": "ALI-S", f"item_{item.id}": "-1",
              "purchased_at": "2026-10-08T10:00", "evidence_ref": "EV"},
        follow_redirects=True,
    )
    assert "Quantidade inválida" in resp.get_data(as_text=True)


def test_mesmo_codigo_outra_compra_mesmo_pedido_conflito(
    db, make_user, make_product
):
    user = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, user, product, quantity=4)
    pa = dispatch.register_manual_purchase(
        **_purchase_args(order, item, external_order_id="ALI-1",
                         items={item.id: 2}))
    db.session.commit()
    pb = dispatch.register_manual_purchase(
        **_purchase_args(order, item, external_order_id="ALI-2",
                         items={item.id: 2}))
    db.session.commit()
    dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=pa.purchase_id,
        tracking_code="BR000000001XX",
        carrier="Correios", evidence_ref="EV-1", items={item.id: 1})
    db.session.commit()
    # Mesmo código na outra compra: conflito, sem escrita parcial.
    with pytest.raises(ValueError, match="tracking_code_other_purchase"):
        dispatch.register_manual_shipment(
            order_id=order.id, purchase_id=pb.purchase_id,
            tracking_code="BR000000001XX",
            carrier="Correios", evidence_ref="EV-2", items={item.id: 1})
    db.session.rollback()
    assert Shipment.query.count() == 1
    assert ShipmentItem.query.count() == 1


def test_evidencia_fora_das_telas_publicas(
    client, db, make_user, make_product, login
):
    owner = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, owner, product)
    res = dispatch.register_manual_purchase(
        **_purchase_args(order, item, evidence_ref="EV-IDENTIFICAVEL-XYZ"))
    db.session.commit()
    dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=res.purchase_id,
        tracking_code="BR123456789XX", carrier="Correios",
        posted_at="2026-10-08T12:00:00", evidence_ref="EV-POST-XYZ",
        items={item.id: 1})
    db.session.commit()
    login(owner.email)
    for path in (f"/encomendas/pedido/{order.id}", "/acompanhar-pedido"):
        html = client.get(path).get_data(as_text=True)
        assert "EV-IDENTIFICAVEL-XYZ" not in html
        assert "EV-POST-XYZ" not in html
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert "BR123456789XX" in html  # código público segue visível


def test_bucket_exige_entrega_total(client, db, make_user, make_product, login):
    from loja.views.routes import _order_purchase_bucket

    owner = make_user()
    product = make_product(stock=10)
    order, item = _paid_order(db, owner, product, quantity=2)
    res = dispatch.register_manual_purchase(**_purchase_args(order, item))
    db.session.commit()
    ship = dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=res.purchase_id,
        tracking_code="BR123456789XX", carrier="Correios",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()
    db.session.expire_all()
    loaded = db.session.get(Order, order.id)
    # Uma remessa entregue cobrindo 1 de 2: NÃO conclui.
    db.session.query(Shipment).filter_by(
        id=ship.shipment_id).update({"status": "entregue"})
    db.session.commit()
    db.session.expire_all()
    assert _order_purchase_bucket(db.session.get(Order, order.id)) == "enviado"
    login(owner.email)
    partial = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert "itens pendentes" in partial
    # Cobertura total entregue: conclui.
    ship2 = dispatch.register_manual_shipment(
        order_id=order.id, purchase_id=res.purchase_id,
        tracking_code="BR987654321XX", carrier="Correios",
        evidence_ref="EV", items={item.id: 1})
    db.session.commit()
    db.session.query(Shipment).filter_by(
        id=ship2.shipment_id).update({"status": "entregue"})
    db.session.commit()
    db.session.expire_all()
    assert _order_purchase_bucket(
        db.session.get(Order, order.id)) == "aguardando_avaliacao"
    html = client.get(f"/encomendas/pedido/{order.id}").get_data(as_text=True)
    assert "itens pendentes" not in html
