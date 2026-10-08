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
from datetime import timedelta

import pytest

from loja.services.stock_reservations import (
    release_reserved_stock,
    release_single_expired_stock_reservation,
    reserved_quantities_by_variant,
)
from loja.services.variant_availability import (
    parse_availability_flag,
    set_variant_availability,
)
from loja.utils import utcnow
from loja.views.payments_routes import _reserve_stock
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


# --------------------------------------------------------------------------
# 4) Restituição x disponibilidade: devolve à variante, nunca ao agregado
# --------------------------------------------------------------------------

def _debit(admin_user, product, variant, quantity):
    """Abate real via checkout (variante ativa, com estoque)."""
    _reserve_stock([{
        "product_id": product.id,
        "product_name": product.name,
        "variant_id": variant.id,
        "quantity": quantity,
        "unit_price": Decimal("71.43"),
    }])


def _reserve_and_disable(admin_catalog, db, admin_user, *, quantity=2):
    """Publica, reserva pela via real, espelha o fornecedor e desabilita."""
    product = _publish_multi(admin_catalog)
    variants = _variants_by_sku(product)
    first = variants[f"{MULTI_ID}-1"]
    _debit(admin_user, product, first, quantity)
    # Espelho que a sincronização técnica gravaria (teto da restituição).
    first.supplier_stock = 5
    variants[f"{MULTI_ID}-2"].supplier_stock = 3
    db.session.commit()
    order = _reserved_order(db, admin_user, product, first, quantity=quantity)
    set_variant_availability(product=product, variant_id=str(first.id), available=False)
    db.session.commit()
    return product, first, order


def test_restituicao_compartilhada_nao_infla_agregado(admin_catalog, db, admin_user):
    product, first, order = _reserve_and_disable(
        admin_catalog, db, admin_user, quantity=2
    )
    assert (first.stock, product.stock) == (3, 3)

    release_reserved_stock(order)
    db.session.commit()

    assert first.stock == 5  # devolvida à variante (teto 5 − 0 outros)
    assert product.stock == 3  # agregado intacto (soma das ATIVAS)
    assert order.stock_reserved is False
    assert order.status == "pending_payment"  # via compartilhada não transiciona

    # Segunda restituição: idempotente, nada muda.
    release_reserved_stock(order)
    db.session.commit()
    assert (first.stock, product.stock) == (5, 3)

    # Reabilitar recalcula do zero e incorpora o que foi devolvido.
    set_variant_availability(product=product, variant_id=str(first.id), available=True)
    db.session.commit()
    assert product.stock == 8


def test_liberacao_por_expiracao_nao_infla_agregado(admin_catalog, db, admin_user):
    product, first, order = _reserve_and_disable(
        admin_catalog, db, admin_user, quantity=2
    )
    order.mp_preference_expires_at = utcnow() - timedelta(hours=1)
    db.session.commit()

    assert release_single_expired_stock_reservation(order.id) is True

    db.session.expire_all()
    variants = _variants_by_sku(_draft_from_db(MULTI_ID))
    assert variants[f"{MULTI_ID}-1"].stock == 5
    assert _draft_from_db(MULTI_ID).stock == 3
    closed = db.session.get(type(order), order.id)
    assert closed.stock_reserved is False and closed.status == "expired"

    # Repetir: inelegível (reserva encerrada), sem mudança.
    assert release_single_expired_stock_reservation(order.id) is False
    db.session.expire_all()
    assert _variants_by_sku(_draft_from_db(MULTI_ID))[f"{MULTI_ID}-1"].stock == 5
    assert _draft_from_db(MULTI_ID).stock == 3

    # Reabilitar incorpora a devolução.
    set_variant_availability(
        product=_draft_from_db(MULTI_ID),
        variant_id=str(variants[f"{MULTI_ID}-1"].id),
        available=True,
    )
    db.session.commit()
    assert _draft_from_db(MULTI_ID).stock == 8


def test_restituicao_respeita_teto_e_outras_reservas(admin_catalog, db, admin_user):
    product = _publish_multi(admin_catalog)
    variants = _variants_by_sku(product)
    first = variants[f"{MULTI_ID}-1"]
    _debit(admin_user, product, first, 2)
    order_a = _reserved_order(db, admin_user, product, first, quantity=2)
    _debit(admin_user, product, first, 1)
    order_b = _reserved_order(db, admin_user, product, first, quantity=1)
    assert first.stock == 2  # 5 − 2 − 1

    # Fornecedor reduziu o estoque depois da reserva.
    first.supplier_stock = 2
    db.session.commit()
    set_variant_availability(product=product, variant_id=str(first.id), available=False)
    db.session.commit()
    assert product.stock == 3

    # Libera A: outras = 3 − 2 = 1 → teto 2 − 1 = 1 (não infla acima).
    release_reserved_stock(order_a)
    db.session.commit()
    assert first.stock == 1
    assert product.stock == 3
    assert order_a.stock_reserved is False
    assert order_b.stock_reserved is True

    # Libera B: outras = 0 → teto 2 → 1 + 1 = 2.
    release_reserved_stock(order_b)
    db.session.commit()
    assert first.stock == 2
    assert product.stock == 3

    set_variant_availability(product=product, variant_id=str(first.id), available=True)
    db.session.commit()
    assert product.stock == 5  # 2 (variante) + 3, dentro do espelho


# --------------------------------------------------------------------------
# 5) Rotas: permissão, sessão, CSRF e método
# --------------------------------------------------------------------------

def _availability_post(client, product_id, variant_id, flag, *, follow=True):
    return client.post(
        f"{PREFIX}/rascunho/{product_id}/disponibilidade",
        data={"variant_id": str(variant_id), "disponivel": flag},
        follow_redirects=follow,
    )


def test_rota_exige_admin(admin_catalog, db, login, make_user):
    product = _import_multi(admin_catalog)
    first = product.variants[0]
    login(make_user(is_admin=False).email)

    resp = _availability_post(admin_catalog, MULTI_ID, first.id, "0")

    assert resp.status_code == 200
    assert "não possui permissão" in resp.get_data(as_text=True)
    db.session.expire_all()
    assert _variants_by_sku(_draft_from_db(MULTI_ID))[first.supplier_sku_id].is_active is True
    assert _draft_from_db(MULTI_ID).stock == 8


def test_rota_exige_login(client):
    # Sem nenhum login neste teste (o `g` do app context reaproveita o
    # usuário da primeira autenticação — mesma convenção de
    # `test_other_routes_require_admin`): o guard dispara antes de
    # qualquer acesso ao banco, sem precisar de rascunho criado.
    for route in (
        f"{PREFIX}/rascunho/{MULTI_ID}/disponibilidade",
        f"{PREFIX}/publicado/{MULTI_ID}/disponibilidade",
    ):
        resp = client.post(route, data={"variant_id": "x", "disponivel": "0"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/login")


def test_rota_exige_sessao_aliexpress(admin_catalog, db, monkeypatch):
    import loja.views.aliexpress_admin_routes as routes

    _import_multi(admin_catalog)
    monkeypatch.setattr(routes, "_resolve_ali_session", lambda: (None, None))
    first = _draft_from_db(MULTI_ID).variants[0]

    resp = _availability_post(admin_catalog, MULTI_ID, first.id, "0")

    assert resp.status_code == 200
    db.session.expire_all()
    assert _variants_by_sku(_draft_from_db(MULTI_ID))[first.supplier_sku_id].is_active is True


def test_rota_csrf_invalido_bloqueia(admin_catalog, app, db, monkeypatch):
    _import_multi(admin_catalog)
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    first = _draft_from_db(MULTI_ID).variants[0]

    resp = _availability_post(admin_catalog, MULTI_ID, first.id, "0", follow=False)

    assert resp.status_code == 400
    db.session.expire_all()
    assert _variants_by_sku(_draft_from_db(MULTI_ID))[first.supplier_sku_id].is_active is True


def test_rota_get_retorna_405(admin_catalog):
    _import_multi(admin_catalog)
    for route in (
        f"{PREFIX}/rascunho/{MULTI_ID}/disponibilidade",
        f"{PREFIX}/publicado/{MULTI_ID}/disponibilidade",
    ):
        assert admin_catalog.get(route).status_code == 405
