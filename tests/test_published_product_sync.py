"""Testes da sincronização TÉCNICA de produto publicado (Etapa 3).

Cobrimos: somente estoque/técnico é atualizado; decisões comerciais
(nome, descrição, imagens, categoria, preço, margem, custo, moeda, frete,
precificação, publicação, origem de envio e seleção nacional) são preservadas;
divergência de custo/moeda é sinalizada e nunca aplicada; produto com estoque
zero NÃO é despublicado; novo SKU entra inativo; SKU sumido é desativado;
conflitos controlados não gravam nada; admin/sessão exigidos; nenhuma chamada
de frete e nenhum token/ID externo nos logs.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from loja.integrations.aliexpress import admin_session
from loja.modules.models import ProductSync
from loja.services.purchase_controls import purchase_block_codes
from loja.services.sales_readiness import evaluate_product_readiness
from tests.test_aliexpress_admin_catalog import (
    ACCESS_TOKEN,
    ADMIN_SESSION_KEY,
    MULTI_ID,
    PREFIX,
    PRODUCT_ID,
    TITLE,
    FakeAliExpressClient,
    _clean_aliexpress_data,  # noqa: F401  (fixture autouse compartilhada)
    _draft_from_db,
    _frete,
    _import,
    _margem,
    _nacional,
    _origem,
    _publicar,
    _set_admin_session,
    _supplier_product_from_db,
    ae_env,  # noqa: F401  (fixture)
    admin_catalog,  # noqa: F401  (fixture)
    catalog,  # noqa: F401  (fixture)
    fake_client,  # noqa: F401  (fixture)
    freight_calls,
    make_detail,
    make_multi_detail,
    reference_quote_table,  # noqa: F401  (fixture autouse compartilhada)
)

SYNC_PATH = f"{PREFIX}/publicado/{PRODUCT_ID}/sincronizar"


def _sync(client, product_id=PRODUCT_ID, *, follow=True):
    return client.post(
        f"{PREFIX}/publicado/{product_id}/sincronizar",
        follow_redirects=follow,
    )


def _publish(client, product_id=PRODUCT_ID, margin="30"):
    _import(client, product_id)
    _frete(client, product_id=product_id)
    _margem(client, product_id=product_id, margin=margin)
    _publicar(client, product_id=product_id)


# --------------------------------------------------------------------------
# 1) Só estoque/técnico; decisões comerciais preservadas
# --------------------------------------------------------------------------

def test_sincroniza_estoque_e_preserva_decisoes_comerciais(
    admin_catalog, db, admin_user
):
    _publish(admin_catalog)
    product = _draft_from_db()
    sp = _supplier_product_from_db(db)

    # Evidência de origem + seleção nacional entram antes da sincronização.
    _origem(admin_catalog, prefix="publicado")
    _nacional(admin_catalog, prefix="publicado",
              selected=[v.id for v in product.variants])

    before = {
        "name": product.name,
        "description": product.description,
        "price": product.price,
        "target_margin_percent": product.target_margin_percent,
        "currency": product.currency,
        "cost": product.cost,
        "pricing_status": product.pricing_status,
        "pricing_error": product.pricing_error,
        "is_draft": product.is_draft,
        "is_active": product.is_active,
        "image1": product.image1,
        "category": product.category,
        "sp_cost": sp.cost,
        "sp_currency": sp.currency,
        "freight_status": sp.freight_status,
        "shipping_cost": sp.shipping_cost,
        "landed_cost": sp.landed_cost,
    }
    variant = sorted(product.variants, key=lambda v: str(v.supplier_sku_id))[0]
    assert variant.stock == 5

    # Mesmo produto, estoque do fornecedor caiu 5 -> 2.
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=2)
    resp = _sync(admin_catalog)
    body = resp.get_data(as_text=True)
    assert "Sincronização técnica concluída" in body
    assert "Estoque: 5 → 2" in body

    db.session.expire_all()
    product = _draft_from_db()
    sp = _supplier_product_from_db(db)
    variant = sorted(product.variants, key=lambda v: str(v.supplier_sku_id))[0]

    assert variant.stock == 2
    assert product.stock == 2

    # Nada comercial foi tocado.
    assert product.name == before["name"] == TITLE
    assert product.description == before["description"]
    assert product.price == before["price"] == Decimal("142.86")
    assert product.target_margin_percent == before["target_margin_percent"]
    assert product.currency == before["currency"] == "BRL"
    assert product.cost == before["cost"]
    assert product.pricing_status == before["pricing_status"] == "priced"
    assert product.pricing_error is None
    assert product.image1 == before["image1"]
    assert product.category == before["category"]
    assert sp.cost == before["sp_cost"]
    assert sp.currency == before["sp_currency"]
    assert sp.freight_status == before["freight_status"] == "available"
    assert sp.shipping_cost == before["shipping_cost"]
    assert sp.landed_cost == before["landed_cost"]

    # Publicação e seleção/origem preservadas.
    assert product.is_draft is False and product.is_active is True
    assert all(v.national_shipping_operation for v in product.variants)
    from loja.services.shipping_origin import resolve_product_summary

    assert resolve_product_summary(product).label == "brazil"
    assert freight_calls() == []


def test_estoque_zero_nao_despublica(admin_catalog, db):
    _publish(admin_catalog)
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=0)

    resp = _sync(admin_catalog)
    body = resp.get_data(as_text=True)
    assert "Sincronização técnica concluída" in body

    db.session.expire_all()
    product = _draft_from_db()
    assert product.stock == 0
    assert product.is_active is True and product.is_draft is False
    variant = product.variants[0]
    assert variant.stock == 0 and variant.is_active is False


def test_novo_sku_entra_inativo(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09"], [5, 3]
    )
    _publish(admin_catalog, MULTI_ID)
    assert len(_draft_from_db(MULTI_ID).variants) == 2

    # Catálogo ganhou um terceiro tamanho: existe, mas NÃO está à venda.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"], [5, 3, 2]
    )
    resp = _sync(admin_catalog, MULTI_ID)
    assert "1 nova(s) opção(ões) inativa(s)" in resp.get_data(as_text=True)

    db.session.expire_all()
    product = _draft_from_db(MULTI_ID)
    variants = {v.supplier_sku_id: v for v in product.variants}
    assert len(variants) == 3
    new_variant = variants[f"{MULTI_ID}-3"]
    assert new_variant.is_active is False
    assert new_variant.stock == 2
    # Estoque agregado só conta variantes ativas (2 vendáveis -> 8).
    assert product.stock == 8
    assert product.is_active is True


def test_sku_sumido_do_catalogo_e_desativado(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09"], [5, 3]
    )
    _publish(admin_catalog, MULTI_ID)

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(["27.59"], [4])
    resp = _sync(admin_catalog, MULTI_ID)
    assert "1 desativada(s) por estoque" in resp.get_data(as_text=True)

    db.session.expire_all()
    product = _draft_from_db(MULTI_ID)
    variants = {v.supplier_sku_id: v for v in product.variants}
    assert len(variants) == 2  # nunca deleta
    gone = variants[f"{MULTI_ID}-2"]
    assert gone.is_active is False and gone.stock == 0
    assert product.stock == 4
    assert product.is_active is True


# --------------------------------------------------------------------------
# 2) Divergência de custo/moeda: sinalizada, nunca aplicada
# --------------------------------------------------------------------------

def test_divergencia_de_custo_sinalizada_sem_aplicar(admin_catalog, db):
    _publish(admin_catalog)
    product = _draft_from_db()
    sp = _supplier_product_from_db(db)
    price_before = product.price

    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(price="120.00", stock=5)
    resp = _sync(admin_catalog)
    body = resp.get_data(as_text=True)
    assert "Custo do fornecedor divergente" in body
    assert "sinalizada SEM aplicar" in body

    db.session.expire_all()
    product = _draft_from_db()
    sp = _supplier_product_from_db(db)
    # Custo/moeda/preço/margem/conteúdo intocados: divergência nunca é
    # aplicada. A aprovação operacional, sim, deixa de valer.
    assert sp.cost == Decimal("80.00")
    assert product.cost == Decimal("80.00")
    assert product.pricing_status == "stale"
    assert product.pricing_error == "supplier_cost_changed"
    assert product.price == price_before == Decimal("142.86")
    assert product.target_margin_percent == Decimal("30.00")
    assert sp.sync_error == "supplier_cost_changed"
    assert product.sync_error == "supplier_cost_changed"
    # Continua publicado (nunca despublica), mas não vende mais.
    assert product.is_active is True
    assert product.is_draft is False
    assert purchase_block_codes(product) == ["supplier_cost_changed"]
    decision = evaluate_product_readiness(product)
    assert "supplier_cost_divergence" in decision.reasons


def test_divergencia_de_moeda_sinalizada_sem_aplicar(admin_catalog, db):
    _publish(admin_catalog)

    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(
        currency="USD", stock=5
    )
    resp = _sync(admin_catalog)
    body = resp.get_data(as_text=True)
    assert "Moeda do fornecedor divergente" in body

    db.session.expire_all()
    product = _draft_from_db()
    sp = _supplier_product_from_db(db)
    assert product.currency == "BRL"
    assert sp.currency == "BRL"
    assert product.pricing_status == "stale"
    assert product.pricing_error == "currency_changed"
    assert product.price == Decimal("142.86")
    assert sp.sync_error == "currency_changed"
    assert product.sync_error == "currency_changed"
    assert product.is_active is True
    assert purchase_block_codes(product) == ["currency_changed"]
    assert freight_calls() == []


def test_sem_divergencia_limpa_sync_error(admin_catalog, db):
    _publish(admin_catalog)
    sp = _supplier_product_from_db(db)
    sp.sync_error = "supplier_cost_changed"
    db.session.commit()

    _sync(admin_catalog)
    db.session.expire_all()
    assert _supplier_product_from_db(db).sync_error is None


# --------------------------------------------------------------------------
# 3) Conflitos controlados: nada é gravado
# --------------------------------------------------------------------------

def test_produto_nao_publicado_nao_sincroniza(admin_catalog, db):
    _import(admin_catalog)  # rascunho
    product = _draft_from_db()
    variant = product.variants[0]
    stock_before = variant.stock
    last_sync_at = _supplier_product_from_db(db).last_sync_at

    resp = _sync(admin_catalog)
    assert "Produto publicado não encontrado" in resp.get_data(as_text=True)

    db.session.expire_all()
    product = _draft_from_db()
    assert product.is_draft is True and product.is_active is False
    assert product.variants[0].stock == stock_before
    # Sem carimbo de sincronização: o rascunho não passou por esta rota.
    assert _supplier_product_from_db(db).last_sync_at == last_sync_at


def test_detalhe_de_outro_produto_nao_sincroniza(admin_catalog, db):
    _publish(admin_catalog)
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(
        product_id="2000000000099", stock=9
    )

    resp = _sync(admin_catalog)
    body = resp.get_data(as_text=True)
    assert "Os dados recebidos são de outro produto" in body

    db.session.expire_all()
    product = _draft_from_db()
    assert product.variants[0].stock == 5
    assert product.stock == 5
    assert _supplier_product_from_db(db).sync_error is None


def test_falha_do_fornecedor_reverte_tudo(admin_catalog, db, monkeypatch):
    import loja.views.aliexpress_admin_routes as routes

    _publish(admin_catalog)
    product = _draft_from_db()

    def _boom(*args, **kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(routes, "sync_published_product", _boom)

    resp = _sync(admin_catalog)
    assert "Nenhuma alteração foi aplicada" in resp.get_data(as_text=True)

    db.session.expire_all()
    product = _draft_from_db()
    assert product.stock == 5
    assert product.is_active is True and product.is_draft is False


def test_sincronizacao_e_idempotente(admin_catalog, db):
    _publish(admin_catalog)
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=2)

    _sync(admin_catalog)
    _sync(admin_catalog)

    db.session.expire_all()
    product = _draft_from_db()
    assert product.stock == 2
    assert len(product.variants) == 1
    sp = _supplier_product_from_db(db)
    assert sp.sync_error is None
    states = ProductSync.query.filter_by(product_id=product.id).all()
    assert len(states) == 1
    # `state` preservado: a sincronização de publicado não muda o estado.
    assert states[0].state == "draft"
    assert states[0].last_synced_at is not None
    assert states[0].error is None


# --------------------------------------------------------------------------
# 4) Segurança: admin, sessão, logs e chamadas de fornecedor
# --------------------------------------------------------------------------

def test_requer_admin(client):
    resp = client.post(SYNC_PATH, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_requer_sessao_aliexpress(admin_catalog, db):
    _publish(admin_catalog)
    admin_catalog.delete_cookie(admin_session.COOKIE_NAME)
    resp = _sync(admin_catalog, follow=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(PREFIX + "/")


def test_log_nao_contem_token_nem_id_externo(admin_catalog, db, caplog):
    _publish(admin_catalog)
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=2)

    with caplog.at_level(logging.INFO):
        _sync(admin_catalog)

    text = caplog.text
    assert ACCESS_TOKEN not in text
    assert PRODUCT_ID not in text
    assert ADMIN_SESSION_KEY not in text
    # Nenhuma chamada de frete/compra em nenhuma etapa.
    assert freight_calls() == []


def test_pagina_publicada_exibe_botao_de_sincronizacao(admin_catalog, db):
    _publish(admin_catalog)
    body = admin_catalog.get(f"{PREFIX}/publicado/{PRODUCT_ID}/frete").get_data(
        as_text=True
    )
    assert "Sincronização técnica" in body
    assert f"/publicado/{PRODUCT_ID}/sincronizar" in body
