"""Testes administrativos da sincronização de status (Fase 8.0.6) — HARD-OFF.

Cobre:
- filtro de estados inclui ``pending_reconciliation`` (além dos 8.0.6);
- POST ``/sincronizar-status`` exige admin (não-admin -> /login, syncer nunca
  chamado);
- admin em ordem elegível (submitted + ali_order_id): chama o boundary e avança;
- admin em ordem inelegível (ready / sem ali_order_id): NÃO chama o boundary;
- provider padrão HARD-OFF: nenhum HTTP, sessão inalterada, flash sanitizado;
- exceções nunca aparecem no HTML (apenas mensagem sanitizada);
- detail renderiza shipped_at/delivered_at quando presentes;
- nenhum segredo (token) vaza na página admin.
"""
import uuid
from decimal import Decimal

import pytest

from loja import db
from loja.integrations.aliexpress.fulfillment import (
    ensure_aliexpress_fulfillment,
)
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusSyncer,
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusResult,
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
    submit_aliexpress_order,
)
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.modules.models import (
    AliExpressOrder,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)

BASE_URL = "/admin/integrations/aliexpress/pedidos"


class OkSubmitter(AliExpressOrderSubmitter):
    def __init__(self):
        self.calls = 0

    def submit(self, request):
        self.calls += 1
        return AliExpressSubmissionResult(
            external_order_id="AE-ADM-%s" % uuid.uuid4().hex[:10]
        )


class FixedSyncer(AliExpressOrderStatusSyncer):
    def __init__(self, outcome=STATUS_CONFIRMED):
        self.outcome = outcome
        self.calls = 0

    def sync(self, request: AliExpressOrderStatusRequest):
        self.calls += 1
        return AliExpressOrderStatusResult(outcome=self.outcome)


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    import socket

    def _reject(*args, **kwargs):
        raise AssertionError("rede bloqueada durante testes admin 8.0.6")

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


@pytest.fixture()
def ae_env(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )
    yield


def _materialize_submitted_return(db, buyer, ae_env, submit=True):
    from loja.integrations.aliexpress.credential_store import persist_credential

    user = buyer
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp Adm 8.0.6",
        channel_type="aliexpress",
        seller_id="SELLER_ADM_806",
        default_currency="USD",
    )
    db.session.add(supplier)
    product = Product(
        id=uuid.uuid4(),
        name="Prod Adm 8.0.6",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="EXT_ADM_806",
        is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.flush()
    sp = SupplierProduct(
        id=uuid.uuid4(),
        supplier_id=supplier.id,
        product_id=product.id,
        supplier_product_id="SP_ADM_806",
        cost=Decimal("10.00"),
        currency="USD",
        is_active=True,
    )
    db.session.add(sp)
    db.session.flush()
    variant = ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id=sp.id,
        supplier_sku_id="SKU_ADM_806",
        sku=None,
        color="Red",
        size="M",
        attributes=[
            {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
            {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
        ],
        stock=10,
        price=Decimal("29.90"),
        is_active=True,
    )
    db.session.add(variant)
    db.session.flush()

    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok806",
        expires_in=3600,
    )
    db.session.flush()

    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="paid",
        subtotal=Decimal("29.90"),
        total=Decimal("29.90"),
        stock_reserved=False,
        customer_name="U",
        customer_email=user.email,
        customer_phone="11999999999",
        zip_code="01001000",
        street="Rua",
        number="1",
        complement="",
        neighborhood="C",
        city="SP",
        state="SP",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=product.id,
            variant_id=variant.id,
            product_name=product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
    )
    db.session.commit()

    results = ensure_aliexpress_fulfillment(order)
    assert results[0].state == "ready"
    db.session.commit()
    ali = AliExpressOrder.query.filter_by(order_id=order.id).first()
    if submit:
        sub = submit_aliexpress_order(ali, submitter=OkSubmitter())
        db.session.commit()
        assert sub.state == "submitted"
    db.session.expire_all()
    return db.session.get(AliExpressOrder, ali.id)


def test_index_requires_admin_login(client):
    resp = client.get(BASE_URL)
    assert resp.headers["Location"].endswith("/login")


def test_filter_inclui_pending_reconciliation(client, admin_user, login, db, ae_env):
    login(admin_user.email)
    resp = client.get(BASE_URL)
    assert resp.status_code == 200
    assert "pending_reconciliation" in resp.text


def test_non_admin_nao_pode_sincronizar(client, regular_user, login, db, ae_env, monkeypatch):
    ali = _materialize_submitted_return(db, regular_user, ae_env)
    from loja.integrations.aliexpress import status_sync as ss

    syncer = FixedSyncer()
    monkeypatch.setattr(ss, "get_default_order_status_syncer", lambda: syncer)
    login(regular_user.email)
    resp = client.post(f"{BASE_URL}/{ali.id}/sincronizar-status", follow_redirects=True)
    # @admin_required redireciona antes de qualquer acionamento do boundary.
    assert "Status sincronizado" not in resp.text
    assert syncer.calls == 0


def test_admin_sincroniza_ordem_elegivel(
    client, admin_user, login, db, ae_env, monkeypatch
):
    ali = _materialize_submitted_return(db, admin_user, ae_env)
    from loja.integrations.aliexpress import status_sync as ss

    syncer = FixedSyncer(STATUS_CONFIRMED)
    monkeypatch.setattr(ss, "get_default_order_status_syncer", lambda: syncer)
    login(admin_user.email)
    resp = client.post(
        f"{BASE_URL}/{ali.id}/sincronizar-status", follow_redirects=True
    )
    assert resp.status_code == 200
    assert "Status sincronizado" in resp.text
    assert syncer.calls == 1
    db.session.expire_all()
    fresh = db.session.get(AliExpressOrder, ali.id)
    assert fresh.state == "confirmed"


def test_admin_ordem_inelegivel_nao_chama_boundary(
    client, admin_user, login, db, ae_env, monkeypatch
):
    ali = _materialize_submitted_return(db, admin_user, ae_env, submit=False)
    from loja.integrations.aliexpress import status_sync as ss

    syncer = FixedSyncer()
    monkeypatch.setattr(ss, "get_default_order_status_syncer", lambda: syncer)
    login(admin_user.email)

    # estado ready -> inelegível
    resp = client.post(
        f"{BASE_URL}/{ali.id}/sincronizar-status", follow_redirects=True
    )
    assert resp.status_code == 200
    assert "não pode ter status" in resp.text
    assert syncer.calls == 0


def test_admin_ordem_sem_ali_order_id_nao_chama_boundary(
    client, admin_user, login, db, ae_env, monkeypatch
):
    ali = _materialize_submitted_return(db, admin_user, ae_env, submit=False)
    from loja.integrations.aliexpress import status_sync as ss

    syncer = FixedSyncer()
    monkeypatch.setattr(ss, "get_default_order_status_syncer", lambda: syncer)
    login(admin_user.email)

    # remove identidade externa -> nunca consulta provider
    ali.ali_order_id = None
    db.session.commit()
    db.session.expire_all()
    ali = db.session.get(AliExpressOrder, ali.id)
    resp = client.post(
        f"{BASE_URL}/{ali.id}/sincronizar-status", follow_redirects=True
    )
    assert "não pode ter status" in resp.text
    assert syncer.calls == 0


def test_admin_hard_off_nao_faz_http_e_flash_sanitizado(
    client, admin_user, login, db, ae_env
):
    ali = _materialize_submitted_return(db, admin_user, ae_env)
    login(admin_user.email)
    resp = client.post(
        f"{BASE_URL}/{ali.id}/sincronizar-status", follow_redirects=True
    )
    assert resp.status_code == 200
    assert "Status sincronizado" in resp.text
    assert "desabilitada" not in resp.text
    db.session.expire_all()
    fresh = db.session.get(AliExpressOrder, ali.id)
    assert fresh.state == "submitted"


def test_detail_nao_vaza_segredos(client, admin_user, login, db, ae_env):
    ali = _materialize_submitted_return(db, admin_user, ae_env)
    login(admin_user.email)
    resp = client.get(f"{BASE_URL}/{ali.id}")
    assert resp.status_code == 200
    assert "tok806" not in resp.text
    assert "access_token" not in resp.text
    assert "client_secret" not in resp.text


def test_detail_renderiza_shipped_e_delivered(
    client, admin_user, login, db, ae_env
):
    ali = _materialize_submitted_return(db, admin_user, ae_env)
    from loja.utils import utcnow

    now = utcnow()
    ali.state = "delivered"
    ali.shipped_at = now
    ali.delivered_at = now
    db.session.commit()
    db.session.expire_all()
    ali = db.session.get(AliExpressOrder, ali.id)
    login(admin_user.email)
    resp = client.get(f"{BASE_URL}/{ali.id}")
    assert resp.status_code == 200
    assert now.strftime("%d/%m/%Y %H:%M") in resp.text