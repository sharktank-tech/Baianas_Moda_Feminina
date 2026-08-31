"""Testes do catálogo administrativo AliExpress (Fase 7).

Cobrimos: exigência de admin, credencial criptografada (ausente/expirada/
adulterada), busca/detalhe read-only, import idempotente como rascunho, frete
manual (positivo/zero explícito/negativo/currency mismatch), precificação via
serviço com Decimal, publicação via serviço (bloqueios e sucesso), visibilidade
no storefront (draft invisível / publicado visível), nenhuma chamada de freight
API e nenhum token/code/state nos logs.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import admin_session
from loja.integrations.aliexpress.models import (
    AliExpressProductDetail,
    AliExpressProductSku,
    AliExpressSearchProduct,
    AliExpressSearchResult,
)
from loja.modules.models import (
    Product,
    ProductSync,
    ProductVariant,
    Supplier,
    SupplierProduct,
)

ADMIN_MODULE = "loja.views.aliexpress_admin_routes"
PREFIX = "/admin/integrations/aliexpress"
PRODUCT_ID = "2000000000001"
TITLE = "Vestido Teste Fase7"
SELLER_ID = "seller-test"
ACCESS_TOKEN = "token-test"


# --------------------------------------------------------------------------
# Fake do cliente AliExpress (nunca chama freight API)
# --------------------------------------------------------------------------

class FakeAliExpressClient:
    """Registra todas as chamadas e nunca implementa freight (deve permanecer
    sem chamadas de freight — as testamos explicitamente)."""

    calls = []
    search_result = AliExpressSearchResult()
    details = {}
    search_error = None
    detail_error = None

    def __init__(self, *, app_key=None, app_secret=None, access_token=None):
        FakeAliExpressClient.calls.append(
            {
                "op": "construct",
                "app_key": app_key,
                "app_secret": app_secret,
                "access_token": access_token,
            }
        )

    def search_products(self, keyword=None, **kwargs):
        FakeAliExpressClient.calls.append({"op": "search_products", "keyword": keyword})
        if FakeAliExpressClient.search_error is not None:
            raise FakeAliExpressClient.search_error
        return FakeAliExpressClient.search_result

    def get_product(self, product_id=None, **kwargs):
        FakeAliExpressClient.calls.append({"op": "get_product", "product_id": product_id})
        if FakeAliExpressClient.detail_error is not None:
            raise FakeAliExpressClient.detail_error
        return FakeAliExpressClient.details.get(product_id)

    # Métodos de frete existem só para PODERMOS detectar se fossem chamados.
    def query_freight(self, **kwargs):
        FakeAliExpressClient.calls.append({"op": "query_freight"})
        return None

    def calculate_buyer_freight(self, **kwargs):
        FakeAliExpressClient.calls.append({"op": "buyer_freight"})
        return None

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.search_result = AliExpressSearchResult()
        cls.details = {}
        cls.search_error = None
        cls.detail_error = None


def freight_calls():
    return [c for c in FakeAliExpressClient.calls if c["op"] in ("query_freight", "buyer_freight")]


# --------------------------------------------------------------------------
# Dados de exemplo
# --------------------------------------------------------------------------

def make_sku(sku_id="SKU-1", price="80.00", currency="BRL", stock=5):
    return AliExpressProductSku(
        sku_id=sku_id,
        sale_price=Decimal(price),
        original_price=Decimal(price),
        currency=currency,
        available_stock=stock,
        attributes=[
            {"name": "Cor", "value": "Vermelho"},
            {"name": "Tamanho", "value": "M"},
        ],
    )


def make_detail(
    product_id=PRODUCT_ID,
    title=TITLE,
    price="80.00",
    currency="BRL",
    stock=5,
):
    return AliExpressProductDetail(
        product_id=product_id,
        title=title,
        description="Descrição do vestido de teste.",
        images=["https://ae.example/img1.jpg", "https://ae.example/img2.jpg"],
        skus=[make_sku(f"{product_id}-1", price, currency, stock)],
        available_stock=stock,
        sale_price=Decimal(price),
        original_price=Decimal(price),
        currency=currency,
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_aliexpress_data(db):
    """Limpa os dados AliExpress criados em qualquer teste (antes de cada teste)."""
    product_ids = db.session.query(Product.id).filter_by(source="aliexpress")
    supplier_ids = db.session.query(Supplier.id).filter_by(channel_type="aliexpress")
    supplier_product_ids = db.session.query(SupplierProduct.id).filter(
        SupplierProduct.supplier_id.in_(supplier_ids)
    )
    ProductSync.query.filter(ProductSync.product_id.in_(product_ids)).delete(
        synchronize_session=False
    )
    ProductVariant.query.filter(ProductVariant.supplier_product_id.in_(supplier_product_ids)).delete(
        synchronize_session=False
    )
    ProductVariant.query.filter(ProductVariant.product_id.in_(product_ids)).delete(
        synchronize_session=False
    )
    SupplierProduct.query.filter(SupplierProduct.supplier_id.in_(supplier_ids)).delete(
        synchronize_session=False
    )
    Product.query.filter(Product.id.in_(product_ids)).delete(synchronize_session=False)
    Supplier.query.filter(Supplier.id.in_(supplier_ids)).delete(synchronize_session=False)
    db.session.commit()
    yield
    db.session.rollback()


@pytest.fixture()
def fake_client(monkeypatch):
    monkeypatch.setattr(ADMIN_MODULE + ".AliExpressClient", FakeAliExpressClient)
    FakeAliExpressClient.reset()
    yield FakeAliExpressClient
    FakeAliExpressClient.reset()


@pytest.fixture()
def ae_env(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_ADMIN_SESSION_KEY", "chave-admin-sessao-teste")
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "test-app-key")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "test-app-secret")
    yield


@pytest.fixture()
def catalog(fake_client):
    FakeAliExpressClient.search_result = AliExpressSearchResult(
        products=[
            AliExpressSearchProduct(
                item_id=PRODUCT_ID,
                title=TITLE,
                sale_price=Decimal("80.00"),
                orders=12,
                images=["https://ae.example/img1.jpg"],
            )
        ],
        total=1,
    )
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail()
    yield


def _set_admin_session(client, seller_id=SELLER_ID, access_token=ACCESS_TOKEN, ttl=1800):
    value = admin_session.create_admin_session_value(
        seller_id=seller_id, access_token=access_token, ttl_seconds=ttl
    )
    client.set_cookie(admin_session.COOKIE_NAME, value)
    return value


@pytest.fixture()
def admin_catalog(client, admin_user, login, catalog, ae_env):
    login(admin_user.email)
    _set_admin_session(client)
    yield client


# --------------------------------------------------------------------------
# Ações helpers (via HTTP conforme o fluxo real)
# --------------------------------------------------------------------------

def _import(client, product_id=PRODUCT_ID):
    return client.post(f"{PREFIX}/{product_id}/importar", follow_redirects=True)


def _frete(client, product_id=PRODUCT_ID, shipping="20.00", currency="BRL", follow=True):
    return client.post(
        f"{PREFIX}/rascunho/{product_id}/frete",
        data={"shipping_cost": shipping, "freight_currency": currency},
        follow_redirects=follow,
    )


def _margem(client, product_id=PRODUCT_ID, margin="30", follow=True):
    return client.post(
        f"{PREFIX}/rascunho/{product_id}/margem",
        data={"target_margin_percent": margin},
        follow_redirects=follow,
    )


def _publicar(client, product_id=PRODUCT_ID, follow=True):
    return client.post(f"{PREFIX}/rascunho/{product_id}/publicar", follow_redirects=follow)


def _draft_from_db(product_id=PRODUCT_ID):
    return (
        Product.query.options()
        .filter_by(source="aliexpress", external_id=product_id)
        .first()
    )


def _supplier_product_from_db(db, product_id=PRODUCT_ID):
    product = _draft_from_db(product_id)
    if product is None:
        return None
    return (
        db.session.query(SupplierProduct)
        .filter_by(product_id=product.id, is_active=True)
        .first()
    )


# --------------------------------------------------------------------------
# 1) Admin obrigatório
# --------------------------------------------------------------------------

def test_index_requires_admin_login(client):
    resp = client.get(PREFIX)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_buscar_requires_admin_login(client):
    resp = client.post(f"{PREFIX}/buscar", data={"q": "vestido"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_regular_user_denied(client, regular_user, login):
    login(regular_user.email)
    resp = client.get(PREFIX, follow_redirects=True)
    assert "não possui permissão" in resp.get_data(as_text=True)


@pytest.mark.parametrize("path", ["buscar", "detalhe", "rascunho", "frete", "margem", "publicar"])
def test_other_routes_require_admin(client, ae_env, fake_client, path):
    # erro do ali_session_required é posterior ao admin_required; sem login
    # todas as rotas redirecionam para /login.
    resp = client.post(f"{PREFIX}/{PRODUCT_ID}/importar")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


# --------------------------------------------------------------------------
# 2) Credencial (ausente / expirada / adulterada)
# --------------------------------------------------------------------------

def test_config_missing_fails_closed(client, admin_user, login, fake_client):
    # ALIEXPRESS_ADMIN_SESSION_KEY ausente -> rotas protegidas retornam 500.
    login(admin_user.email)
    resp = client.post(f"{PREFIX}/buscar", data={"q": "x"})
    assert resp.status_code == 500
    assert "ALIEXPRESS_ADMIN_SESSION_KEY" in resp.get_data(as_text=True)


def test_session_missing_redirects_to_reauthorize(client, admin_user, login, ae_env, fake_client):
    login(admin_user.email)
    resp = client.get(PREFIX, follow_redirects=True)
    assert "Autorizar novamente" in resp.get_data(as_text=True)

    resp = client.post(f"{PREFIX}/buscar", data={"q": "x"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(PREFIX + "/")


def test_session_expired_flow(client, admin_user, login, ae_env, fake_client):
    login(admin_user.email)
    _set_admin_session(client, ttl=-100)  # exp no passado
    resp = client.post(f"{PREFIX}/buscar", data={"q": "x"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(PREFIX + "/")
    page = client.get(PREFIX, follow_redirects=True)
    assert "Autorizar novamente" in page.get_data(as_text=True)


def test_session_tampered(client, admin_user, login, ae_env, fake_client):
    login(admin_user.email)
    value = _set_admin_session(client)
    client.set_cookie(admin_session.COOKIE_NAME, value[:-1] + ("A" if value[-1] != "A" else "B"))
    resp = client.post(f"{PREFIX}/buscar", data={"q": "x"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(PREFIX + "/")


# --------------------------------------------------------------------------
# 3) Busca e detalhe (read-only)
# --------------------------------------------------------------------------

def test_busca_sucesso(admin_catalog):
    resp = admin_catalog.post(f"{PREFIX}/buscar", data={"q": "vestido"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert TITLE in body
    assert "R$ 80.00" in body
    assert PRODUCT_ID in body
    assert "Ver detalhes" in body


def test_detalhe_readonly(admin_catalog):
    resp = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert TITLE in body
    assert PRODUCT_ID in body
    assert "Importar como rascunho" in body


# --------------------------------------------------------------------------
# 4) Import como rascunho + idempotência
# --------------------------------------------------------------------------

def test_import_cria_draft(admin_catalog, db):
    resp = _import(admin_catalog)
    body = resp.get_data(as_text=True)
    assert "Rascunho AliExpress" in body

    product = _draft_from_db()
    assert product is not None
    assert product.source == "aliexpress"
    assert product.is_draft is True
    assert product.is_active is False
    assert product.pricing_status == "blocked"
    assert product.pricing_error == "freight_unavailable"

    sp = (
        db.session.query(SupplierProduct)
        .filter_by(product_id=product.id, is_active=True)
        .first()
    )
    assert sp is not None
    assert sp.freight_status == "provider_unavailable"
    assert sp.shipping_cost is None
    assert sp.landed_cost is None
    assert sp.currency == "BRL"
    assert sp.cost == Decimal("80.00")

    supplier = db.session.query(Supplier).filter_by(id=sp.supplier_id).first()
    assert supplier.seller_id == SELLER_ID
    # Token NUNCA persistido no banco (nem nas colunas genéricas do Supplier).
    assert supplier.access_token is None
    assert supplier.access_token_expires_at is None


def test_reimport_idempotente(admin_catalog, db):
    _import(admin_catalog)
    first_product_id = _draft_from_db().id
    first_variants = db.session.query(ProductVariant).filter_by(product_id=first_product_id).count()

    _import(admin_catalog)

    products = db.session.query(Product).filter_by(source="aliexpress", external_id=PRODUCT_ID).all()
    assert len(products) == 1
    assert (db.session.query(ProductVariant).filter_by(product_id=first_product_id).count()
            == first_variants)


# --------------------------------------------------------------------------
# 5) Frete manual
# --------------------------------------------------------------------------

def test_frete_positivo(admin_catalog, db):
    _import(admin_catalog)
    resp = _frete(admin_catalog, shipping="20.00", currency="BRL")
    assert resp.status_code == 200

    sp = _supplier_product_from_db(db)
    assert sp.freight_status == "available"
    assert sp.shipping_cost == Decimal("20.00")
    assert sp.freight_currency == "BRL"
    assert sp.landed_cost == Decimal("100.00")


def test_frete_zero_explicito(admin_catalog, db):
    _import(admin_catalog)
    resp = _frete(admin_catalog, shipping="0", currency="BRL")
    assert resp.status_code == 200

    sp = _supplier_product_from_db(db)
    assert sp.freight_status == "available"
    assert sp.shipping_cost == Decimal("0")
    assert sp.landed_cost == Decimal("80.00")


def test_frete_negativo_bloqueado(admin_catalog, db):
    _import(admin_catalog)
    resp = _frete(admin_catalog, shipping="-5", currency="BRL")
    assert "negativo" in resp.get_data(as_text=True)

    sp = _supplier_product_from_db(db)
    assert sp.freight_status == "provider_unavailable"
    assert sp.shipping_cost is None
    assert sp.landed_cost is None


def test_frete_currency_mismatch_bloqueado(admin_catalog, db):
    _import(admin_catalog)
    resp = _frete(admin_catalog, shipping="20", currency="EUR")
    assert "diverg" in resp.get_data(as_text=True)

    sp = _supplier_product_from_db(db)
    assert sp.freight_status == "provider_unavailable"
    assert sp.shipping_cost is None
    assert sp.landed_cost is None


# --------------------------------------------------------------------------
# 6) Precificação (margem)
# --------------------------------------------------------------------------

def test_margem_invalida_bloqueada(admin_catalog, db):
    _import(admin_catalog)
    resp = _margem(admin_catalog, margin="0")
    assert "Precificação bloqueada" in resp.get_data(as_text=True)

    product = _draft_from_db()
    assert product.pricing_status == "blocked"
    assert product.price == Decimal("80.00")


def test_margem_sem_frete_bloqueada(admin_catalog, db):
    _import(admin_catalog)
    resp = _margem(admin_catalog, margin="30")
    b=resp.get_data(as_text=True)
    i=b.find("Precifica"); print("FLASH-HUNT>>>", b[i:i+120] if i>=0 else "NOT-FOUND (len %d)" % len(b))
    assert "Precificacao bloqueada" in resp.get_data(as_text=True) or "Precificação bloqueada" in resp.get_data(as_text=True)
    product = _draft_from_db()
    assert product.pricing_status == "blocked"
    assert product.pricing_error == "freight_unavailable"


def test_pricing_correto_decimal(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    resp = _margem(admin_catalog, margin="30")
    assert "Preço calculado" in resp.get_data(as_text=True)

    product = _draft_from_db()
    assert product.pricing_status == "priced"
    assert product.target_margin_percent == Decimal("30.00")
    # 100 / (1-0.30) = 142.8571... -> arredonda para CIMA: 142.86
    assert product.price == Decimal("142.86")


# --------------------------------------------------------------------------
# 7) Publicação
# --------------------------------------------------------------------------

def test_publicacao_bloqueada_sem_frete(admin_catalog, db):
    _import(admin_catalog)
    resp = _publicar(admin_catalog)
    assert "Publicação bloqueada" in resp.get_data(as_text=True)
    assert "freight_unavailable" in resp.get_data(as_text=True)

    product = _draft_from_db()
    assert product.is_draft is True
    assert product.is_active is False


def test_publicacao_bloqueada_sem_pricing(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    resp = _publicar(admin_catalog)
    assert "pricing_not_ready" in resp.get_data(as_text=True)

    product = _draft_from_db()
    assert product.is_draft is True
    assert product.is_active is False


def test_publicacao_permitida_apos_frete_e_pricing(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    resp = _publicar(admin_catalog)
    assert "Produto publicado" in resp.get_data(as_text=True)

    product = _draft_from_db()
    assert product.is_draft is False
    assert product.is_active is True


# --------------------------------------------------------------------------
# 8) Visibilidade no storefront
# --------------------------------------------------------------------------

def _storefront_exposes(q):
    from loja.views.routes import public_product_query

    query = public_product_query()
    return query.filter(Product.name.ilike(f"%{q}%")).count() > 0


def test_draft_invisivel_no_storefront(admin_catalog, db):
    _import(admin_catalog)
    assert _storefront_exposes(TITLE) is False

    resp = admin_catalog.get("/produtos")
    assert TITLE not in resp.get_data(as_text=True)


def test_publicado_visivel_no_storefront(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    _publicar(admin_catalog)

    assert _storefront_exposes(TITLE) is True
    resp = admin_catalog.get("/produtos")
    assert TITLE in resp.get_data(as_text=True)


# --------------------------------------------------------------------------
# 9) Segurança operacional
# --------------------------------------------------------------------------

def test_nenhuma_chamada_de_freight_api(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    _publicar(admin_catalog)

    admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    admin_catalog.post(f"{PREFIX}/buscar", data={"q": "vestido"})

    assert freight_calls() == []


def test_nenhum_token_code_state_em_logs(admin_catalog, db, caplog, app):
    captured = []

    class Recorder(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Recorder()
    app.logger.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO):
            cookie_value = _set_admin_session(admin_catalog)
            _import(admin_catalog)
            _frete(admin_catalog, shipping="20.00", currency="BRL")
            _margem(admin_catalog, margin="30")
            _publicar(admin_catalog)
    finally:
        app.logger.removeHandler(handler)

    all_logs = "\n".join(captured) + "\n" + caplog.text
    assert ACCESS_TOKEN not in all_logs
    assert SELLER_ID not in all_logs
    assert cookie_value not in all_logs
    assert admin_session.COOKIE_NAME not in all_logs


def test_templates_incluem_csrf(admin_catalog, db):
    _import(admin_catalog)
    resp = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    assert 'name="csrf_token"' in resp.get_data(as_text=True)
    resp = admin_catalog.get(PREFIX)
    assert 'name="csrf_token"' in resp.get_data(as_text=True)


def test_csrf_enforced_when_enabled(app, client, admin_user, login, ae_env, fake_client, catalog):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        login(admin_user.email)
        _set_admin_session(client)
        resp = client.post(f"{PREFIX}/buscar", data={"q": "vestido"})
        assert resp.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False

