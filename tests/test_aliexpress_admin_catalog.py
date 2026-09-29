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
# Chave Fernet NATIVA válida para os testes (gerada com Fernet.generate_key()).
# Nunca é imprimida nos logs; apenas exercita o caminho de configuração.
ADMIN_SESSION_KEY = "5ssrAWjAL2KymHrLzGxXHTkkpw-SYiIcEt2ELfkKvnA="


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
    monkeypatch.setenv("ALIEXPRESS_ADMIN_SESSION_KEY", ADMIN_SESSION_KEY)
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


@pytest.fixture(autouse=True)
def reference_quote_table(db):
    from sqlalchemy import text

    db.session.execute(
        text(
            """CREATE TABLE IF NOT EXISTS supplier_reference_freight_quotes (
                supplier_product_id VARCHAR(36) NOT NULL,
                dest_prefix VARCHAR(3) NOT NULL,
                option_key VARCHAR(120) NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL,
                currency VARCHAR(3) NOT NULL,
                cost NUMERIC(12,2) NOT NULL,
                min_days INTEGER,
                max_days INTEGER,
                quoted_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                provider_method VARCHAR(120) NOT NULL,
                evidence_ref_hash VARCHAR(64),
                updated_at DATETIME NOT NULL,
                dest_cep VARCHAR(8),
                dest_cep_end VARCHAR(8),
                variant_scope VARCHAR(64),
                covers_all_variants BOOLEAN NOT NULL DEFAULT 0,
                qty_max INTEGER,
                supplier_unit_cost NUMERIC, import_duty NUMERIC, icms NUMERIC,
                PRIMARY KEY (supplier_product_id, dest_prefix, option_key, quantity),
                CHECK (length(dest_prefix) = 3),
                CHECK (quantity > 0),
                CHECK (currency = 'BRL'),
                CHECK (cost >= 0),
                CHECK (expires_at > quoted_at)
            )"""
        )
    )
    db.session.execute(text("DELETE FROM supplier_reference_freight_quotes"))
    db.session.commit()
    yield


# --------------------------------------------------------------------------
# Ações helpers (via HTTP conforme o fluxo real)
# --------------------------------------------------------------------------

def _import(client, product_id=PRODUCT_ID):
    return client.post(f"{PREFIX}/{product_id}/importar", follow_redirects=True)


def _frete(client, product_id=PRODUCT_ID, shipping="20.00", currency="BRL", follow=True,
           dest_cep="77000000", ttl_days="30", evidence_ref="COT-TEST-001",
           variant_scope="all", qty_max="1", extra=None):
    data = {
        "shipping_cost": shipping, "freight_currency": currency,
        "supplier_unit_cost": str(Product.query.filter_by(external_id=product_id).first().cost),
        "import_duty": "0.00", "icms": "0.00",
        "freight_dest_cep": dest_cep, "freight_ttl_days": ttl_days,
        "freight_evidence_ref": evidence_ref,
        "freight_variant_scope": variant_scope, "freight_qty_max": qty_max,
    }
    if extra:
        data.update(extra)
    return client.post(
        f"{PREFIX}/rascunho/{product_id}/frete",
        data=data,
        follow_redirects=follow,
    )


def _renovar(client, product_id=PRODUCT_ID, shipping="20.00", currency="BRL", follow=True,
             dest_cep="77000000", ttl_days="30", evidence_ref="COT-TEST-001",
             variant_scope="all", qty_max="1", extra=None):
    data = {
        "shipping_cost": shipping, "freight_currency": currency,
        "supplier_unit_cost": str(Product.query.filter_by(external_id=product_id).first().cost),
        "import_duty": "0.00", "icms": "0.00",
        "freight_dest_cep": dest_cep, "freight_ttl_days": ttl_days,
        "freight_evidence_ref": evidence_ref,
        "freight_variant_scope": variant_scope, "freight_qty_max": qty_max,
    }
    if extra:
        data.update(extra)
    return client.post(
        f"{PREFIX}/publicado/{product_id}/frete",
        data=data,
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


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", PREFIX),
        ("POST", f"{PREFIX}/buscar"),
        ("GET", f"{PREFIX}/{PRODUCT_ID}/detalhe"),
        ("POST", f"{PREFIX}/{PRODUCT_ID}/importar"),
        ("GET", f"{PREFIX}/rascunho/{PRODUCT_ID}"),
        ("POST", f"{PREFIX}/rascunho/{PRODUCT_ID}/frete"),
        ("POST", f"{PREFIX}/rascunho/{PRODUCT_ID}/margem"),
        ("POST", f"{PREFIX}/rascunho/{PRODUCT_ID}/publicar"),
        ("POST", f"{PREFIX}/encerrar"),
    ],
)
def test_other_routes_require_admin(client, method, path):
    # cada rota do blueprint é exigida autenticada: @admin_required dispara
    # antes de qualquer validação de sessão AliExpress; sem login, redireciona
    # para /login (e nunca executa buscas/escritas).
    resp = client.open(path, method=method)
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


def test_config_invalid_key_fails_closed(client, admin_user, login, fake_client, monkeypatch):
    # Chave presente porém NÃO Fernet válida -> fail-closed (500 sanitizado).
    monkeypatch.setenv("ALIEXPRESS_ADMIN_SESSION_KEY", "nao-e-uma-chave-fernet")
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


def test_detalhe_mostra_faixa_de_preco_dos_skus(admin_catalog):
    # SKUs 27.59/39.09/46.60 -> faixa "BRL 27,59 – 46,60" (vírgula decimal);
    # NUNCA R$ 0,00.
    FakeAliExpressClient.details[PRODUCT_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    resp = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    body = resp.get_data(as_text=True)
    assert "R$ 0.00" not in body
    assert "R$ 0,00" not in body
    assert "BRL 27,59" in body
    assert "46,60" in body
    assert "27,59" in body and "46,60" in body


def test_detalhe_preco_unico_mostra_so_um_valor(admin_catalog):
    # Um único SKU/preço -> "BRL 80,00" (sem faixa " – ").
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail()
    resp = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    body = resp.get_data(as_text=True)
    assert "BRL 80,00" in body
    assert "–" not in body.split("BRL 80,00")[0].split("</span>")[-1]


def test_detalhe_moeda_vem_dos_skus_nao_do_global(admin_catalog):
    # O `set` dentro do loop NÃO persiste fora; a moeda exibida deve vir da
    # moeda dos SKUs (BRL) via namespace, e NÃO da moeda global do produto.
    detail = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    detail.currency = "CNY"  # moeda global diverge dos SKUs
    FakeAliExpressClient.details[PRODUCT_ID] = detail
    resp = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    body = resp.get_data(as_text=True)
    assert "BRL 27,59" in body
    assert "CNY 27,59" not in body
    assert "CNY" not in body or "Moeda: CNY" not in body


def test_detalhe_sem_preco_nao_mostra_zero(admin_catalog):
    # Nenhum SKU/preço global -> "Preço indisponível" (nunca 0,00).
    sku = AliExpressProductSku(
        sku_id="SKU-X",
        sale_price=None,
        original_price=None,
        currency="USD",
        available_stock=3,
        attributes=[],
    )
    detail = AliExpressProductDetail(
        product_id=PRODUCT_ID,
        title=TITLE,
        description="sem preço",
        images=[],
        skus=[sku],
        available_stock=3,
        sale_price=None,
        original_price=None,
        currency="BRL",
    )
    FakeAliExpressClient.details[PRODUCT_ID] = detail
    resp = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    body = resp.get_data(as_text=True)
    assert "R$ 0.00" not in body
    assert "R$ 0,00" not in body
    assert "Preço indisponível" in body


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
# 4b) Cost basis seguro para multivariantes (Fase 7.1)
# --------------------------------------------------------------------------

MULTI_ID = "2000000000009"


def make_multi_detail(sku_prices, stocks, product_id=MULTI_ID):
    """múltiplos SKUs com preços e estoques independentes."""
    skus = [
        make_sku(f"{product_id}-{i}", price, "BRL", stock)
        for i, (price, stock) in enumerate(zip(sku_prices, stocks), start=1)
    ]
    detail = make_detail(product_id=product_id)
    detail.skus = skus
    return detail


def _set_detail(product_id, detail):
    FakeAliExpressClient.details[product_id or PRODUCT_ID] = detail


def make_multi_detail_with_stock(sku_prices, stocks, product_id=MULTI_ID):
    skus = [
        AliExpressProductSku(
            sku_id=f"{product_id}-{i}",
            sale_price=Decimal(price),
            original_price=Decimal(price),
            currency="BRL",
            available_stock=stock,
            attributes=[{"name": "Cor", "value": f"Cor {i}"}],
        )
        for i, (price, stock) in enumerate(zip(sku_prices, stocks), start=1)
    ]
    detail = make_detail(product_id=product_id)
    detail.skus = skus
    return detail


def test_reimport_draft_existente_single_com_multivariante_eleva_cost_basis(
    admin_catalog, db
):
    # Draft real antigo: 1 SKU 27.59 => price=27.59, cost=27.59, blocked.
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(
        product_id=PRODUCT_ID, price="27.59", stock=2
    )
    _import(admin_catalog)
    product = _draft_from_db()
    sp = _supplier_product_from_db(db)
    assert product.price == Decimal("27.59")
    assert sp.cost == Decimal("27.59")
    assert product.pricing_status == "blocked"
    assert product.pricing_error == "freight_unavailable"

    # Reimport agora multivariante: preço provisório preservado (menor),
    # cost basis sobe para o maior SKU ativo (custo seguro).
    FakeAliExpressClient.details[PRODUCT_ID] = make_multi_detail_with_stock(
        ["27.59", "39.09", "46.60"], [5, 3, 2], product_id=PRODUCT_ID
    )
    _import(admin_catalog)

    db.session.refresh(product)
    db.session.refresh(sp)
    assert product.price == Decimal("27.59")  # provisório preservado/menor
    assert product.cost == Decimal("46.60")
    assert sp.cost == Decimal("46.60")
    assert product.pricing_status == "blocked"
    assert product.pricing_error == "freight_unavailable"
    assert product.is_draft is True
    assert product.is_active is False


def test_reimport_apos_priced_new_max_invalida_pricing(admin_catalog, db):
    # Preço antes: importa 46.60 max, frete 10, margem 30 -> priced.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)
    _frete(admin_catalog, product_id=MULTI_ID, shipping="10", currency="BRL")
    _margem(admin_catalog, product_id=MULTI_ID, margin="30")

    product = Product.query.filter_by(source="aliexpress", external_id=MULTI_ID).first()
    sp = _supplier_product_from_db(db, MULTI_ID)
    assert product.pricing_status == "priced"
    assert sp.cost == Decimal("46.60")
    commercial_price = product.price  # preservado após invalidação
    commercial_margin = product.target_margin_percent

    # Reimport com max SKU maior (46.60 -> 50.00): cost basis sobe.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "50.00"],
        [5, 3, 2],
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)

    db.session.refresh(product)
    db.session.refresh(sp)
    assert sp.cost == Decimal("50.00")
    assert product.cost == Decimal("50.00")
    assert product.price == commercial_price  # preservado p/ auditoria
    assert product.target_margin_percent == commercial_margin
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"


def test_cost_basis_ignora_skus_sem_estoque(admin_catalog, db):
    # SKU caro (50.00) com estoque 0 NÃO entra no cost basis: é o máximo
    # apenas entre SKUs realmente vendáveis/ativos (estoque > 0).
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail_with_stock(
        ["27.59", "39.09", "50.00"],
        [5, 3, 0],
        product_id=MULTI_ID,
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)

    sp = _supplier_product_from_db(db, MULTI_ID)
    product = Product.query.filter_by(source="aliexpress", external_id=MULTI_ID).first()
    # max ativo = 39.09 (o 50.00 está sem estoque); catalog price (menor) = 27.59
    assert sp.cost == Decimal("39.09")
    assert product.cost == Decimal("39.09")
    assert product.price == Decimal("27.59")

    # A variante sem estoque existe porém INATIVA.
    inactive = ProductVariant.query.filter_by(
        supplier_product_id=sp.id, is_active=False
    ).first()
    assert inactive is not None
    assert inactive.stock == 0
    assert inactive.price == Decimal("50.00")


def test_draft_multivariante_cost_basis_seguro(admin_catalog, db):
    # SKUs 27.59 / 39.09 / 46.60 (todos ativos) -> custo = MAIOR (46.60);
    # Product.price provisório = MENOR (27.59).
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    resp = admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)
    assert "Rascunho AliExpress" in resp.get_data(as_text=True)

    product = (
        Product.query.filter_by(source="aliexpress", external_id=MULTI_ID).first()
    )
    sp = _supplier_product_from_db(db, MULTI_ID)
    assert sp is not None
    assert sp.cost == Decimal("46.60")
    assert product.cost == Decimal("46.60")
    assert product.price == Decimal("27.59")
    assert product.pricing_status == "blocked"
    assert product.is_draft is True
    assert product.is_active is False


def test_draft_multivariante_nenhum_custo_ativos_acima_do_basis(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)

    sp = _supplier_product_from_db(db, MULTI_ID)
    max_active_price = Decimal("-1")
    variants = (
        ProductVariant.query.filter_by(
            supplier_product_id=sp.id, is_active=True
        ).all()
    )
    assert len(variants) == 3
    for v in variants:
        assert v.price <= sp.cost
        if v.price is not None:
            max_active_price = max(max_active_price, v.price)
    assert max_active_price == Decimal("46.60")
    assert max_active_price == sp.cost


def test_draft_multivariante_cost_basis_usado_no_pricing(admin_catalog, db):
    # landed = cost(46.60) + frete(10.00) = 56.60; margem 30%
    # preço venda = 56.60 / 0.70 = 80.8571... -> arredonda CIMA: 80.86.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)
    _frete(admin_catalog, product_id=MULTI_ID, shipping="10", currency="BRL")
    resp = _margem(admin_catalog, product_id=MULTI_ID, margin="30")
    assert "Preço calculado" in resp.get_data(as_text=True)

    product = (
        Product.query.filter_by(source="aliexpress", external_id=MULTI_ID).first()
    )
    sp = _supplier_product_from_db(db, MULTI_ID)
    assert sp.landed_cost == Decimal("56.60")
    assert product.pricing_status == "priced"
    assert product.price == Decimal("80.86")
    assert product.target_margin_percent == Decimal("30.00")


def test_reimport_sku_max_aumentado_invalida_pricing(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"],
        [5, 3, 2],
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)
    _frete(admin_catalog, product_id=MULTI_ID, shipping="10", currency="BRL")
    _margem(admin_catalog, product_id=MULTI_ID, margin="30")

    product = (
        Product.query.filter_by(source="aliexpress", external_id=MULTI_ID).first()
    )
    assert product.pricing_status == "priced"
    sp = _supplier_product_from_db(db, MULTI_ID)
    assert sp.cost == Decimal("46.60")

    # Reimport com SKU máximo maior => cost basis sobe => pricing invalida.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "51.20"],
        [5, 3, 2],
    )
    admin_catalog.post(f"{PREFIX}/{MULTI_ID}/importar", follow_redirects=True)

    db.session.refresh(product)
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"
    assert product.price == Decimal("80.86")  # preservado p/ auditoria
    assert product.target_margin_percent == Decimal("30.00")

    db.session.refresh(sp)
    assert sp.cost == Decimal("51.20")

    # Publicação bloqueada até reprecificar.
    resp = _publicar(admin_catalog, product_id=MULTI_ID)
    assert "Publicação bloqueada" in resp.get_data(as_text=True)
    assert "pricing_not_ready" in resp.get_data(as_text=True)


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
    assert "Precificação bloqueada" in resp.get_data(as_text=True)
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
# 8) Mudança material de frete invalida precificação (stale/freight_changed)
# --------------------------------------------------------------------------

def test_frete_altera_precificacao_para_stale_e_reprice(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")

    product = _draft_from_db()
    assert product.pricing_status == "priced"
    assert product.price == Decimal("142.86")
    assert product.target_margin_percent == Decimal("30.00")

    # Mudança material do frete: landed recalculado; preço/margem PRESERVADOS;
    # precificação marcada como stale/freight_changed (nunca reprecifica sozinha).
    resp = _frete(admin_catalog, shipping="10", currency="BRL")
    assert resp.status_code == 200
    assert "Frete salvo e landed cost recalculado." in resp.get_data(as_text=True)

    db.session.refresh(product)
    assert product.pricing_status == "stale"
    assert product.pricing_error == "freight_changed"
    assert product.price == Decimal("142.86")
    assert product.target_margin_percent == Decimal("30.00")

    sp = _supplier_product_from_db(db)
    assert sp.freight_status == "available"
    assert sp.shipping_cost == Decimal("10.00")
    assert sp.landed_cost == Decimal("90.00")

    # Publicação bloqueada até reprecificar explicitamente.
    resp = _publicar(admin_catalog)
    assert "Publicação bloqueada" in resp.get_data(as_text=True)
    assert "pricing_not_ready" in resp.get_data(as_text=True)

    db.session.refresh(product)
    assert product.is_draft is True
    assert product.is_active is False

    # Reprecificar explicitamente => novo preço (90 / 0.70 = 128.5714 ->
    # arredondado para CIMA: 128.58) e status priced.
    resp = _margem(admin_catalog, margin="30")
    assert "Preço calculado" in resp.get_data(as_text=True)

    db.session.refresh(product)
    assert product.pricing_status == "priced"
    assert product.pricing_error is None
    assert product.price == Decimal("128.58")

    # Agora a publicação é permitida.
    resp = _publicar(admin_catalog)
    assert "Produto publicado" in resp.get_data(as_text=True)

    db.session.refresh(product)
    assert product.is_draft is False
    assert product.is_active is True


def test_frete_identico_nao_invalida_precificacao(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    product = _draft_from_db()
    assert product.pricing_status == "priced"

    # Re-submissão dos MESMOS valores: nenhuma mudança material -> segue priced.
    _frete(admin_catalog, shipping="20.00", currency="BRL")

    db.session.refresh(product)
    assert product.pricing_status == "priced"
    assert product.pricing_error is None
    assert product.price == Decimal("142.86")


# --------------------------------------------------------------------------
# 9) Publicação congela o rascunho (imutabilidade)
# --------------------------------------------------------------------------

def _product_sp_state(db):
    product = _draft_from_db()
    if product is None:
        return None
    sp = _supplier_product_from_db(db)
    return {
        "price": product.price,
        "target_margin_percent": product.target_margin_percent,
        "pricing_status": product.pricing_status,
        "pricing_error": product.pricing_error,
        "is_active": product.is_active,
        "is_draft": product.is_draft,
        "product_cost": product.cost,
        "sp_cost": sp.cost if sp else None,
        "sp_currency": sp.currency if sp else None,
        "freight_status": sp.freight_status if sp else None,
        "shipping_cost": sp.shipping_cost if sp else None,
        "freight_currency": sp.freight_currency if sp else None,
        "landed_cost": sp.landed_cost if sp else None,
        "sync_status": sp.sync_status if sp else None,
    }


def test_publicado_recusa_carregamento_no_rascunho(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    _publicar(admin_catalog)

    resp = admin_catalog.get(f"{PREFIX}/rascunho/{PRODUCT_ID}", follow_redirects=True)
    assert "não encontrado" in resp.get_data(as_text=True)


def test_publicado_imutavel_aos_posts_da_url_antiga(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    _publicar(admin_catalog)

    before = _product_sp_state(db)
    assert before["is_active"] is True
    assert before["is_draft"] is False

    # POSTs usando a URL antiga do rascunho não editam o publicado: todas
    # redirecionam (rascunho não encontrado) e nenhuma coluna muda.
    for attempt in (
        lambda: _frete(admin_catalog, shipping="10", currency="BRL", follow=False),
        lambda: _margem(admin_catalog, margin="50", follow=False),
        lambda: _publicar(admin_catalog, follow=False),
    ):
        resp = attempt()
        assert resp.status_code == 302

    db.session.expire_all()
    after = _product_sp_state(db)
    assert after == before


# --------------------------------------------------------------------------
# 10) Visibilidade no storefront
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
# 11) Segurança operacional
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



# --------------------------------------------------------------------------
# 11) Evidência de frete obrigatória + renovação de publicado (P7, ITENS 1/2/4)
# --------------------------------------------------------------------------

def _quote_rows(db, sp_id):
    from sqlalchemy import text

    return db.session.execute(
        text(
            "SELECT dest_cep, dest_cep_end, variant_scope, covers_all_variants,"
            " qty_max, cost FROM supplier_reference_freight_quotes"
            " WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).all()


def test_frete_sem_referencia_e_sem_validade_rejeitado(admin_catalog, db):
    """ITEM 1.4: sem referência/validade, falha com mensagem clara e nada grava."""
    _import(admin_catalog)
    resp = admin_catalog.post(
        f"{PREFIX}/rascunho/{PRODUCT_ID}/frete",
        data={"shipping_cost": "20.00", "freight_currency": "BRL"},
        follow_redirects=True,
    )
    page = resp.get_data(as_text=True)
    assert "Obrigatória" in page or "obrigatória" in page.lower()

    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost is None
    assert sp.freight_status != "available"
    assert _quote_rows(db, sp.id) == []


def test_frete_sem_cep_rejeitado(admin_catalog, db):
    _import(admin_catalog)
    resp = admin_catalog.post(
        f"{PREFIX}/rascunho/{PRODUCT_ID}/frete",
        data={
            "shipping_cost": "20.00", "freight_currency": "BRL",
            "freight_ttl_days": "30", "freight_evidence_ref": "COT-X",
            "freight_variant_scope": "all", "freight_qty_max": "1",
        },
        follow_redirects=True,
    )
    assert "CEP" in resp.get_data(as_text=True)
    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost is None
    assert _quote_rows(db, sp.id) == []


def test_frete_com_evidencia_grava_custo_e_cotacao(admin_catalog, db):
    """ITEM 5.1: fluxo real via POST grava custo + cotação com escopo."""
    _import(admin_catalog)
    resp = _frete(
        admin_catalog, shipping="20.00", currency="BRL",
        dest_cep="77000000", ttl_days="30", evidence_ref="COT-REAL-001",
        variant_scope="all", qty_max="1",
        extra={"freight_dest_cep_end": "77099999",
               "freight_min_days": "8", "freight_max_days": "14"},
    )
    page = resp.get_data(as_text=True)
    assert "vigente até" in page

    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost == Decimal("20.00")
    assert sp.freight_status == "available"
    rows = _quote_rows(db, sp.id)
    assert len(rows) == 1
    assert rows[0][0] == "77000000"
    assert rows[0][1] == "77099999"
    assert rows[0][3] == 1  # covers_all_variants
    assert rows[0][4] == 1  # qty_max
    assert Decimal(str(rows[0][5])) == Decimal("20.00")


def test_renovacao_publicado_renova_sem_despublicar(admin_catalog, db):
    """ITEM 2.3/5.2: publicado renova pela rota nova; rascunho segue bloqueado."""
    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    _margem(admin_catalog, margin="30")
    _publicar(admin_catalog)

    product = _draft_from_db()
    assert product.is_active is True and product.is_draft is False
    old_price = product.price

    # Fluxo antigo continua bloqueado para o publicado.
    resp = _frete(admin_catalog, shipping="10", currency="BRL", follow=True)
    assert "não encontrado" in resp.get_data(as_text=True).lower()

    # Renovação pela rota nova, com regras iguais (custo + evidência).
    resp = _renovar(
        admin_catalog, shipping="22.00", currency="BRL",
        dest_cep="77000000", ttl_days="15", evidence_ref="COT-RENOV-002",
        variant_scope="all", qty_max="1",
    )
    page = resp.get_data(as_text=True)
    assert "vigente até" in page
    assert "mesma margem" in page

    db.session.expire_all()
    product = _draft_from_db()
    assert product.is_active is True and product.is_draft is False
    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost == Decimal("22.00")
    # CHECK do banco exige publicado+priced: mudança material recalcula com
    # a MESMA margem (30%) em vez de marcar stale; preço sobe.
    assert product.pricing_status == "priced"
    assert product.pricing_error is None
    assert product.target_margin_percent == Decimal("30.00")
    assert product.price > old_price
    rows = _quote_rows(db, sp.id)
    assert len(rows) == 1
    assert Decimal(str(rows[0][5])) == Decimal("22.00")


def test_renovacao_rascunho_ou_inexistente_bloqueia(admin_catalog, db):
    _import(admin_catalog)
    # Ainda rascunho: renovação de publicado não se aplica.
    resp = _renovar(admin_catalog, follow=True)
    assert "não encontrado" in resp.get_data(as_text=True).lower()
    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost is None

    # ID inexistente.
    resp = admin_catalog.post(
        f"{PREFIX}/publicado/nao-existe/frete",
        data={"shipping_cost": "1.00"},
        follow_redirects=True,
    )
    assert "não encontrado" in resp.get_data(as_text=True).lower()


def test_falha_na_cotacao_reverte_custo(admin_catalog, db, monkeypatch):
    """ITEM 4.3: falha na 2ª gravação reverte a 1ª (transação única)."""
    import loja.views.aliexpress_admin_routes as routes
    import loja.services.supplier_freight_quotes as sfq

    _import(admin_catalog)
    _frete(admin_catalog, shipping="20.00", currency="BRL")
    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost == Decimal("20.00")

    real_store = sfq.store_reference_freight_quote

    def _boom(*args, **kwargs):
        raise RuntimeError("falha simulada na persistência")

    monkeypatch.setattr(sfq, "store_reference_freight_quote", _boom)
    monkeypatch.setattr(routes, "store_reference_freight_quote", _boom,
                        raising=False)

    resp = _frete(admin_catalog, shipping="99.00", currency="BRL")
    page = resp.get_data(as_text=True)
    assert "Nenhuma alteração foi salva" in page

    db.session.expire_all()
    sp = _supplier_product_from_db(db)
    # Custo novo revertido: checkout segue com o par antigo consistente.
    assert sp.shipping_cost == Decimal("20.00")
    rows = _quote_rows(db, sp.id)
    assert len(rows) == 1
    assert Decimal(str(rows[0][5])) == Decimal("20.00")



@pytest.mark.parametrize('missing', ['supplier_unit_cost', 'import_duty', 'icms'])
def test_complete_quote_requires_each_explicit_component(admin_catalog, db, missing):
    _import(admin_catalog)
    response = _frete(admin_catalog, extra={missing: ''})
    assert 'Informe custo do produto' in response.get_data(as_text=True)
    sp = _supplier_product_from_db(db)
    assert sp.shipping_cost is None
    assert sp.import_duty is None and sp.icms is None
    assert _quote_rows(db, sp.id) == []


def test_published_tax_change_reprices_same_margin_atomically(admin_catalog, db):
    _import(admin_catalog)
    _frete(admin_catalog)
    _margem(admin_catalog, margin='30')
    _publicar(admin_catalog)
    response = _renovar(admin_catalog, shipping='0', extra={
        'supplier_unit_cost': '337.85', 'import_duty': '35.05', 'icms': '76.42'})
    assert 'vigente até' in response.get_data(as_text=True)
    db.session.expire_all()
    sp = _supplier_product_from_db(db)
    product = _draft_from_db()
    assert sp.landed_cost == Decimal('449.32')
    assert product.price == Decimal('641.89')
    assert product.target_margin_percent == Decimal('30')
    assert product.is_active and not product.is_draft


def test_purchase_tax_storage_failure_rolls_back_every_amount(admin_catalog, db, monkeypatch):
    import loja.services.supplier_freight_quotes as quotes
    _import(admin_catalog)
    _frete(admin_catalog)
    _margem(admin_catalog, margin='30')
    _publicar(admin_catalog)
    original_price = _draft_from_db().price
    original_cost = _supplier_product_from_db(db).cost
    def fail(**kwargs):
        raise quotes.ReferenceQuoteUnavailable('storage_error')
    monkeypatch.setattr(quotes, 'store_reference_freight_quote', fail)
    _renovar(admin_catalog, shipping='0', extra={
        'supplier_unit_cost': '337.85', 'import_duty': '35.05', 'icms': '76.42'})
    db.session.expire_all()
    sp = _supplier_product_from_db(db)
    assert sp.cost == original_cost
    assert sp.import_duty == 0 and sp.icms == 0
    assert _draft_from_db().price == original_price
