"""Disponibilidade comercial por variante (liga/desliga para venda).

Cobre o controle do administrador que habilita/desabilita UMA variante
para venda preservando registros e vínculos: pedidos, reservas e estoque
da variante ficam intactos; só `is_active` muda (e o agregado decorrente
`Product.stock`, recalculado pela regra canônica de soma das ativas).

Garantias:
- desabilitar impede NOVAS compras sem apagar histórico;
- reativar é explícito no admin — sincronização e reimportação NUNCA
  reativam variante desativada (só desativam a que zerou no fornecedor);
- rota valida pertencimento ao produto e é estrita no formulário;
- agregado usa a soma canônica de variantes ATIVAS (inclui reservas).
"""
from decimal import Decimal

import pytest

from loja.services.stock_reservations import reserved_quantities_by_variant
from loja.services.variant_availability import (
    parse_availability_flag,
    set_variant_availability,
)
from tests.test_aliexpress_admin_catalog import (  # noqa: F401  (fixtures)
    MULTI_ID,
    PREFIX,
    PRODUCT_ID,
    FakeAliExpressClient,
    _clean_aliexpress_data,
    _draft_from_db,
    _import,
    admin_catalog,
    ae_env,
    catalog,
    fake_client,
    make_multi_detail,
    reference_quote_table,
)
from tests.test_published_product_sync import (  # noqa: F401  (helpers)
    _publish,
    _sync,
)
from tests.test_published_sync_resilience import _reserved_order

TWO_SKUS = (["27.59", "39.09"], [5, 3])


def _publish_multi(admin_catalog):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)
    return _draft_from_db(MULTI_ID)


def _import_multi(admin_catalog):
    """Rascunho com 2 variantes (sem frete/margem/publicação)."""
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _import(admin_catalog, MULTI_ID)
    return _draft_from_db(MULTI_ID)


def _variants_by_sku(product):
    return {v.supplier_sku_id: v for v in product.variants}


# --------------------------------------------------------------------------
# 1) Serviço: desabilitar recalcula o agregado, preserva a variante
# --------------------------------------------------------------------------

def test_desabilitar_preserva_variante_e_recaucula_agregado(admin_catalog, db):
    product = _publish_multi(admin_catalog)
    assert product.stock == 8
    first, second = product.variants[0], product.variants[1]

    result = set_variant_availability(
        product=product, variant_id=str(first.id), available=False
    )
    db.session.commit()

    assert result.is_active is False
    assert (result.stock_before, result.stock_after) == (8, 3)
    assert first.is_active is False
    assert first.stock == 5
    assert second.is_active is True
    assert product.stock == 3


def test_reativar_soma_a_variante_de_volta(admin_catalog, db):
    product = _publish_multi(admin_catalog)
    first = product.variants[0]

    set_variant_availability(product=product, variant_id=str(first.id), available=False)
    db.session.commit()
    assert product.stock == 3

    result = set_variant_availability(
        product=product, variant_id=str(first.id), available=True
    )
    db.session.commit()

    assert result.is_active is True
    assert (result.stock_before, result.stock_after) == (3, 8)
    assert first.is_active is True and product.stock == 8


def test_variante_de_outro_produto_rejeitada(admin_catalog, db):
    multi = _publish_multi(admin_catalog)
    _publish(admin_catalog, PRODUCT_ID)
    single = _draft_from_db(PRODUCT_ID)
    foreign = multi.variants[0]

    with pytest.raises(ValueError):
        set_variant_availability(
            product=single, variant_id=str(foreign.id), available=False
        )
    db.session.rollback()
    assert foreign.is_active is True and multi.stock == 8


def test_variant_id_invalido_ou_ausente_rejeitado(admin_catalog, db):
    product = _publish_multi(admin_catalog)
    with pytest.raises(ValueError):
        set_variant_availability(
            product=product, variant_id="nao-e-uuid", available=False
        )
    with pytest.raises(ValueError):
        set_variant_availability(product=product, variant_id=None, available=False)
    db.session.rollback()
    assert product.stock == 8


def test_parse_availability_flag_estrito():
    assert parse_availability_flag("1") is True
    assert parse_availability_flag("0") is False
    for raw in (None, "", "on", "off", "yes", "true", "2", "00", "11"):
        assert parse_availability_flag(raw) is None
    # Tolerância a espaço em branco de formulário é aceitável.
    assert parse_availability_flag(" 1 ") is True


# --------------------------------------------------------------------------
# 2) Rotas admin: alternância com pertencimento e formulário estrito
# --------------------------------------------------------------------------

def test_rota_rascunho_alterna_e_preserva_historico(admin_catalog, db):
    product = _import_multi(admin_catalog)
    assert product.stock == 8
    first = product.variants[0]

    resp = admin_catalog.post(
        f"{PREFIX}/rascunho/{MULTI_ID}/disponibilidade",
        data={"variant_id": str(first.id), "disponivel": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Fora de venda" in resp.get_data(as_text=True)

    db.session.expire_all()
    first = _variants_by_sku(_draft_from_db(MULTI_ID))[first.supplier_sku_id]
    assert first.is_active is False
    assert first.stock == 5

    resp = admin_catalog.post(
        f"{PREFIX}/rascunho/{MULTI_ID}/disponibilidade",
        data={"variant_id": str(first.id), "disponivel": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.expire_all()
    first = _variants_by_sku(_draft_from_db(MULTI_ID))[first.supplier_sku_id]
    assert first.is_active is True
    assert _draft_from_db(MULTI_ID).stock == 8


def test_rota_rejeita_variante_de_outro_produto(admin_catalog, db):
    multi = _import_multi(admin_catalog)
    _import(admin_catalog, PRODUCT_ID)
    single = _draft_from_db(PRODUCT_ID)
    assert len(single.variants) == 1
    foreign = multi.variants[0]

    resp = admin_catalog.post(
        f"{PREFIX}/rascunho/{PRODUCT_ID}/disponibilidade",
        data={"variant_id": str(foreign.id), "disponivel": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert foreign.is_active is True and multi.stock == 8


def test_rota_flag_invalido_nao_altera_nada(admin_catalog, db):
    product = _import_multi(admin_catalog)
    first = product.variants[0]

    resp = admin_catalog.post(
        f"{PREFIX}/rascunho/{MULTI_ID}/disponibilidade",
        data={"variant_id": str(first.id), "disponivel": "on"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert first.is_active is True and product.stock == 8


def test_rota_publicado_alterna(admin_catalog, db):
    product = _publish_multi(admin_catalog)
    first = product.variants[0]

    resp = admin_catalog.post(
        f"{PREFIX}/publicado/{MULTI_ID}/disponibilidade",
        data={"variant_id": str(first.id), "disponivel": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.expire_all()
    first = _variants_by_sku(_draft_from_db(MULTI_ID))[first.supplier_sku_id]
    assert first.is_active is False
    assert _draft_from_db(MULTI_ID).stock == 3


# --------------------------------------------------------------------------
# 3) Compra bloqueada, reserva ativa intacta; sync nunca reativa
# --------------------------------------------------------------------------

def test_nova_compra_bloqueada_mas_reserva_ativa_sobrevive(
    admin_catalog, db, admin_user
):
    from loja.views.payments_routes import _reserve_stock

    product = _publish_multi(admin_catalog)
    first = product.variants[0]
    order = _reserved_order(db, admin_user, product, first, quantity=2)

    set_variant_availability(product=product, variant_id=str(first.id), available=False)
    db.session.commit()

    assert reserved_quantities_by_variant([first.id])[first.id] == 2
    assert order.stock_reserved is True

    with pytest.raises(ValueError):
        _reserve_stock([{
            "product_id": product.id,
            "product_name": product.name,
            "variant_id": first.id,
            "quantity": 1,
            "unit_price": Decimal("71.43"),
        }])
    db.session.rollback()

    set_variant_availability(product=product, variant_id=str(first.id), available=True)
    db.session.commit()
    assert _draft_from_db(MULTI_ID).stock == 8


def test_sync_nao_reativa_variante_desabilitada(admin_catalog, db):
    product = _publish_multi(admin_catalog)
    variants = _variants_by_sku(product)
    first = variants[f"{MULTI_ID}-1"]

    set_variant_availability(product=product, variant_id=str(first.id), available=False)
    db.session.commit()

    # Fornecedor segue com a variante em estoque: o sync atualiza dados
    # técnicos sem tocar na decisão comercial do admin.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _sync(admin_catalog, MULTI_ID)

    db.session.expire_all()
    variants = _variants_by_sku(_draft_from_db(MULTI_ID))
    assert variants[f"{MULTI_ID}-1"].is_active is False
    assert variants[f"{MULTI_ID}-2"].is_active is True
    assert _draft_from_db(MULTI_ID).stock == 3


def test_reimportar_nao_reativa_variante_desabilitada(admin_catalog, db):
    product = _publish_multi(admin_catalog)
    first = product.variants[0]
    set_variant_availability(product=product, variant_id=str(first.id), available=False)
    db.session.commit()

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _import(admin_catalog, MULTI_ID)

    db.session.expire_all()
    variants = _variants_by_sku(_draft_from_db(MULTI_ID))
    assert variants[first.supplier_sku_id].is_active is False
    assert _draft_from_db(MULTI_ID).stock == 3
