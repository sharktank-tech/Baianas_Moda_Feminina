"""Revisão PR #123 — resiliência da sincronização de produto publicado.

Pontos 3–6 da revisão, sem mockar os controles canônicos:

* completude do catálogo NUNCA presumida pela presença de SKUs (resposta
  parcial não desativa SKU ausente e fica registrada com código estável);
* estoque recalculado subtraindo a contabilidade de reservas de pedidos
  (pedido já submetido ao fornecedor não é debitado duas vezes);
* TODA tentativa de sincronização persistida (aplicada/rejeitada/falha/
  erro de API) e exibida na página de renovação;
* cobertura de origem restrita aos SKUs explicitamente cobertos no momento
  da escrita (SKU novo não herda a origem BR da cobertura total).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.published_sync import (
    CATALOG_CENSUS_EMPTY,
    CATALOG_CENSUS_UNPROVEN,
    CATALOG_COUNT_MISMATCH,
    CATALOG_STOCK_MISMATCH,
    catalog_census,
)
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.modules.models import AliExpressOrder, AliExpressSyncAttempt
from loja.services.shipping_origin import (
    covered_supplier_skus,
    load_origin_rows,
    record_manual_confirmation,
    resolve_rows,
)
from loja.utils import utcnow
from tests.test_aliexpress_admin_catalog import (  # noqa: F401  (fixtures)
    MULTI_ID,
    PREFIX,
    PRODUCT_ID,
    FakeAliExpressClient,
    _clean_aliexpress_data,
    _draft_from_db,
    _origem,
    _supplier_product_from_db,
    ae_env,
    admin_catalog,  # noqa: F401
    catalog,  # noqa: F401
    fake_client,  # noqa: F401
    make_detail,
    make_multi_detail,
    make_sku,
    reference_quote_table,  # noqa: F401
)
from tests.test_published_product_sync import (  # noqa: F401  (fixtures)
    _publish,
    _sync,
)

TWO_SKUS = (["27.59", "39.09"], [5, 3])


def _attempts(product_id=PRODUCT_ID):
    product = _draft_from_db(product_id)
    return list(product.sync_attempts or [])


def _reserved_order(db, user, product, variant, *, quantity=2, submitted=False):
    """Pedido com reserva ativa (e, opcionalmente, fulfillment submetido)."""
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
        subtotal=Decimal("142.86"),
        shipping=Decimal("0.00"),
        discount=Decimal("0.00"),
        total=Decimal("142.86"),
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
            variant_id=variant.id,
            product_name=product.name,
            selected_size="M",
            quantity=quantity,
            unit_price=Decimal("71.43"),
            line_total=Decimal("71.43") * quantity,
        )
    )
    db.session.add(order)
    db.session.flush()
    if submitted:
        db.session.add(
            AliExpressOrder(
                id=uuid.uuid4(),
                order_id=order.id,
                supplier_id=None,
                state="submitted",
                attempt_count=1,
                submitted_at=utcnow(),
            )
        )
    db.session.commit()
    return order


# --------------------------------------------------------------------------
# 3) Completude do catálogo nunca presumida pela presença de SKUs
# --------------------------------------------------------------------------

def test_resposta_sem_prova_nao_desativa_sku_sumido(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)
    assert len(_draft_from_db(MULTI_ID).variants) == 2

    # Só 1 SKU chega e a resposta NÃO declara nem contagem nem agregado.
    partial = make_multi_detail(["27.59"], [4])
    partial.available_stock = None
    FakeAliExpressClient.details[MULTI_ID] = partial

    resp = _sync(admin_catalog, MULTI_ID)
    body = resp.get_data(as_text=True)
    assert "sem completude comprovada" in body
    assert CATALOG_CENSUS_UNPROVEN in body

    db.session.expire_all()
    product = _draft_from_db(MULTI_ID)
    variants = {v.supplier_sku_id: v for v in product.variants}
    assert len(variants) == 2
    # SKU presente continua atualizando normalmente.
    assert variants[f"{MULTI_ID}-1"].stock == 4
    # SKU ausente NÃO foi desativado por uma resposta parcial.
    absent = variants[f"{MULTI_ID}-2"]
    assert absent.is_active is True and absent.stock == 3
    assert product.stock == 7

    attempt = _attempts(MULTI_ID)[0]
    assert attempt.status == AliExpressSyncAttempt.STATUS_APPLIED
    assert attempt.catalog_code == CATALOG_CENSUS_UNPROVEN


def test_agregado_divergente_nao_desativa_sku_sumido(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    # Agregado declarado (99) não confere com a soma observada (4).
    partial = make_multi_detail(["27.59"], [4])
    partial.available_stock = 99
    FakeAliExpressClient.details[MULTI_ID] = partial

    resp = _sync(admin_catalog, MULTI_ID)
    assert CATALOG_STOCK_MISMATCH in resp.get_data(as_text=True)

    db.session.expire_all()
    variants = {
        v.supplier_sku_id: v for v in _draft_from_db(MULTI_ID).variants
    }
    assert variants[f"{MULTI_ID}-2"].is_active is True
    assert _attempts(MULTI_ID)[0].catalog_code == CATALOG_STOCK_MISMATCH


def test_contagem_declarada_divergente_nao_desativa(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    partial = make_multi_detail(["27.59"], [4])
    partial.available_stock = None
    partial.raw = {"ae_item_sku_info_dtos": {"sku_count": 3}}
    FakeAliExpressClient.details[MULTI_ID] = partial

    resp = _sync(admin_catalog, MULTI_ID)
    assert CATALOG_COUNT_MISMATCH in resp.get_data(as_text=True)

    db.session.expire_all()
    variants = {
        v.supplier_sku_id: v for v in _draft_from_db(MULTI_ID).variants
    }
    assert variants[f"{MULTI_ID}-2"].is_active is True
    assert _attempts(MULTI_ID)[0].catalog_code == CATALOG_COUNT_MISMATCH


def test_total_de_base_info_dto_nao_e_contagem_de_skus():
    """`ae_item_base_info_dto.total` NUNCA conta como total de SKUs.

    O bug pareava fonte e chaves por índice: sem o container de SKUs, a
    base virava o índice 0 e recebia as chaves genéricas (`total`), então
    um total de itens do anúncio provava completude falsa e autorizava
    desativar SKUs ausentes. Nome genérico só vale dentro do container.
    """
    detail = make_detail()  # 1 SKU observado
    detail.available_stock = None
    detail.raw = {"ae_item_base_info_dto": {"total": 7}}

    census = catalog_census(detail)
    assert census.complete is False
    assert census.code == CATALOG_CENSUS_UNPROVEN
    assert census.declared is None

    # O mesmo nome genérico DENTRO do container de SKUs continua valendo.
    proven = make_detail()
    proven.raw = {"ae_item_sku_info_dtos": {"total": 1}}
    census = catalog_census(proven)
    assert census.complete is True
    assert census.declared == 1


def test_catalogo_vazio_nunca_e_provado_pela_ausencia_de_skus():
    """Resposta sem SKUs não prova "catálogo vazio" (fail-closed)."""
    detail = make_detail()
    detail.skus = []
    detail.available_stock = None

    census = catalog_census(detail)
    assert census.complete is False
    assert census.code == CATALOG_CENSUS_EMPTY
    assert census.observed == 0


def test_completude_comprovada_continua_desativando(admin_catalog, db):
    """Controle: agregado coerente prova completude e a desativação vale."""
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(["27.59"], [4])
    resp = _sync(admin_catalog, MULTI_ID)
    assert CATALOG_CENSUS_UNPROVEN not in resp.get_data(as_text=True)

    db.session.expire_all()
    variants = {
        v.supplier_sku_id: v for v in _draft_from_db(MULTI_ID).variants
    }
    assert variants[f"{MULTI_ID}-2"].is_active is False
    assert _attempts(MULTI_ID)[0].catalog_code is None


# --------------------------------------------------------------------------
# 4) Estoque subtrai a contabilidade de reservas
# --------------------------------------------------------------------------

def test_sincronizacao_nao_devolve_estoque_reservado(
    admin_catalog, db, admin_user
):
    _publish(admin_catalog)
    product = _draft_from_db()
    variant = product.variants[0]
    assert variant.stock == 5

    # Pedido pendente ainda debitou 2 unidades.
    _reserved_order(db, admin_user, product, variant, quantity=2)

    # Fornecedor reporta o TOTAL (5): a reserva local segue debitada.
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=5)
    resp = _sync(admin_catalog)
    assert "Estoque: 5 → 3" in resp.get_data(as_text=True)

    db.session.expire_all()
    product = _draft_from_db()
    assert product.variants[0].stock == 3
    assert product.stock == 3


def test_pedido_ja_submetido_ao_fornecedor_nao_duplica_baixa(
    admin_catalog, db, admin_user
):
    _publish(admin_catalog)
    product = _draft_from_db()
    variant = product.variants[0]

    # Fulfillment já submetido: o abatimento já consta no estoque do
    # fornecedor, subtrair de novo duplicaria a baixa.
    _reserved_order(db, admin_user, product, variant, quantity=2, submitted=True)

    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=5)
    resp = _sync(admin_catalog)
    assert "Estoque: 5 → 5" in resp.get_data(as_text=True)

    db.session.expire_all()
    assert _draft_from_db().variants[0].stock == 5


def test_registro_submetido_sem_pedido_nao_anula_reservas(
    admin_catalog, db, admin_user
):
    """`AliExpressOrder` submetido com `order_id` NULL não zera a contagem.

    `NOT IN` com NULL anularia TODAS as reservas e a sync devolveria
    estoque que um pedido pendente ainda debitou (fail-open proibido).
    """
    from loja.services.stock_reservations import reserved_quantities_by_variant

    _publish(admin_catalog)
    product = _draft_from_db()
    variant = product.variants[0]
    _reserved_order(db, admin_user, product, variant, quantity=2)
    db.session.add(
        AliExpressOrder(
            id=uuid.uuid4(),
            order_id=None,
            state="submitted",
            submitted_at=utcnow(),
        )
    )
    db.session.commit()

    assert reserved_quantities_by_variant([variant.id]) == {variant.id: 2}

    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=5)
    resp = _sync(admin_catalog)
    assert "Estoque: 5 → 3" in resp.get_data(as_text=True)


def test_estoque_do_fornecedor_menor_que_reserva_fica_em_zero(
    admin_catalog, db, admin_user
):
    """Reserva maior que o estoque reportado nunca vira negativo."""
    _publish(admin_catalog)
    product = _draft_from_db()
    variant = product.variants[0]

    _reserved_order(db, admin_user, product, variant, quantity=5)

    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=2)
    _sync(admin_catalog)

    db.session.expire_all()
    refreshed = _draft_from_db().variants[0]
    assert refreshed.stock == 0
    assert refreshed.is_active is False


def test_liberacao_nunca_devolve_acima_do_estoque_do_fornecedor(
    admin_catalog, db, admin_user
):
    """Fornecedor reduziu o estoque: a liberação respeita o teto.

    A sync grava ``max(S - reservas, 0)`` e o espelho ``supplier_stock``.
    Sem o teto, a liberação de uma reserva vencida faria
    ``0 + 2 = 2`` acima do ``S = 1`` que o fornecedor ainda tem — estoque
    fantasma vendável. Com o teto, a devolução é
    ``min(atual + qtd, S - outras reservas) = 1``.
    """
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    _publish(admin_catalog)
    product = _draft_from_db()
    variant = product.variants[0]
    order = _reserved_order(db, admin_user, product, variant, quantity=2)
    order.mp_preference_expires_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    order_id = order.id

    # O fornecedor cai de 5 para 1; com 2 reservadas, local vira 0 e o
    # espelho registra o total do fornecedor (1).
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=1)
    _sync(admin_catalog)

    db.session.expire_all()
    synced = _draft_from_db().variants[0]
    assert synced.stock == 0
    assert synced.supplier_stock == 1

    assert release_single_expired_stock_reservation(order_id, source="test")

    db.session.expire_all()
    product = _draft_from_db()
    variant = product.variants[0]
    # Teto = fornecedor(1) − outras reservas(0) = 1: nunca 2. A variante foi
    # desativada pelo sync (zerada no fornecedor), então a devolução restaura
    # o estoque DELA sem entrar no agregado (soma das ATIVAS = 0).
    assert variant.is_active is False
    assert variant.stock == 1
    assert variant.supplier_stock == 1
    assert product.stock == 0

    # Reativação explícita incorpora o devolvido.
    from loja.services.variant_availability import set_variant_availability

    set_variant_availability(
        product=product, variant_id=str(variant.id), available=True
    )
    db.session.commit()
    assert _draft_from_db().stock == 1


def test_liberacao_com_fornecedor_estavel_devolve_integral(
    admin_catalog, db, admin_user
):
    """Controle: com o teto igual ao total, a devolução segue integral."""
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    _publish(admin_catalog)
    product = _draft_from_db()
    variant = product.variants[0]
    order = _reserved_order(db, admin_user, product, variant, quantity=2)
    order.mp_preference_expires_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    order_id = order.id

    FakeAliExpressClient.details[PRODUCT_ID] = make_detail(stock=5)
    _sync(admin_catalog)

    db.session.expire_all()
    synced = _draft_from_db().variants[0]
    assert synced.stock == 3
    assert synced.supplier_stock == 5

    assert release_single_expired_stock_reservation(order_id, source="test")

    db.session.expire_all()
    variant = _draft_from_db().variants[0]
    assert variant.stock == 5
    assert variant.supplier_stock == 5


# --------------------------------------------------------------------------
# 6) Toda tentativa é persistida (e aparece na página de renovação)
# --------------------------------------------------------------------------

def test_tentativa_aplicada_e_persistida(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09"], [4, 3]
    )
    _sync(admin_catalog, MULTI_ID)

    attempts = _attempts(MULTI_ID)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == AliExpressSyncAttempt.STATUS_APPLIED
    assert attempt.variants_updated == 1
    assert attempt.variants_deactivated == 0
    assert attempt.stock_before == 8
    assert attempt.stock_after == 7
    assert attempt.divergence is None

    # A página de renovação mostra a história persistida.
    resp = admin_catalog.get(f"{PREFIX}/publicado/{MULTI_ID}/frete")
    html = resp.get_data(as_text=True)
    assert "Tentativas recentes" in html
    assert "applied" in html


def test_tentativa_rejeitada_persiste_codigo(admin_catalog, db):
    _publish(admin_catalog)
    FakeAliExpressClient.details[PRODUCT_ID] = None

    resp = _sync(admin_catalog)
    assert "Produto não encontrado no catálogo" in resp.get_data(as_text=True)

    attempts = _attempts(PRODUCT_ID)
    assert len(attempts) == 1
    assert attempts[0].status == AliExpressSyncAttempt.STATUS_REJECTED
    assert attempts[0].code == "product_not_found"


def test_tentativa_recusada_por_ausencia_de_preco_persiste(admin_catalog, db):
    _publish(admin_catalog)
    broken = make_detail()
    broken.skus = [make_sku(f"{PRODUCT_ID}-1", "80.00", "BRL", None)]
    broken.skus[0].sale_price = None
    broken.skus[0].original_price = None
    FakeAliExpressClient.details[PRODUCT_ID] = broken

    resp = _sync(admin_catalog)
    assert "Nenhum SKU com preço no fornecedor" in resp.get_data(as_text=True)

    attempts = _attempts(PRODUCT_ID)
    assert attempts[0].status == AliExpressSyncAttempt.STATUS_REJECTED
    assert attempts[0].code == "missing_supplier_price"


def test_tentativa_com_erro_de_api_persiste(admin_catalog, db):
    _publish(admin_catalog)
    FakeAliExpressClient.detail_error = AliExpressAPIError(
        "limite atingido", code="40001"
    )

    resp = _sync(admin_catalog)
    assert resp.status_code in (302, 200)

    attempts = _attempts(PRODUCT_ID)
    assert attempts[0].status == AliExpressSyncAttempt.STATUS_API_ERROR
    assert attempts[0].code == "40001"


def test_falha_inesperada_persiste(admin_catalog, db, monkeypatch):
    _publish(admin_catalog)

    import loja.views.aliexpress_admin_routes as routes

    def _boom(**kwargs):
        raise RuntimeError("bug inesperado")

    monkeypatch.setattr(routes, "sync_published_product", _boom)

    resp = _sync(admin_catalog)
    assert "Falha na sincronização" in resp.get_data(as_text=True)

    attempts = _attempts(PRODUCT_ID)
    assert attempts[0].status == AliExpressSyncAttempt.STATUS_FAILED
    assert attempts[0].code == "unexpected_error"
    # Nada foi aplicado.
    db.session.expire_all()
    assert _draft_from_db().variants[0].stock == 5


def test_historico_acumula_tentativas(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(["27.59"], [4])
    partial = FakeAliExpressClient.details[MULTI_ID]
    partial.available_stock = None
    _sync(admin_catalog, MULTI_ID)

    FakeAliExpressClient.details[MULTI_ID] = None
    _sync(admin_catalog, MULTI_ID)

    attempts = _attempts(MULTI_ID)
    assert [a.status for a in attempts] == [
        AliExpressSyncAttempt.STATUS_REJECTED,
        AliExpressSyncAttempt.STATUS_APPLIED,
    ]
    # Mais recente primeiro (backref ordenado por created_at desc).
    assert attempts[0].code == "product_not_found"


# --------------------------------------------------------------------------
# 5) Cobertura da origem restrita aos SKUs cobertos na escrita
# --------------------------------------------------------------------------

def _origin_rows(db, product_id=MULTI_ID):
    supplier_product = _supplier_product_from_db(db, product_id)
    rows = load_origin_rows([supplier_product.id])
    return supplier_product, rows.get(str(supplier_product.id), [])


def test_sku_novo_nao_herda_cobertura_de_origem(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    # Confirmação de cobertura total feita AGORA cobre só os 2 SKUs reais.
    supplier_product = _supplier_product_from_db(db, MULTI_ID)
    row = record_manual_confirmation(
        supplier_product_id=supplier_product.id,
        origin="brazil",
        evidence_ref="ORIGEM-COB-001",
        covers_all=True,
    )
    db.session.commit()
    assert covered_supplier_skus(row) == {f"{MULTI_ID}-1", f"{MULTI_ID}-2"}

    # Catálogo ganha um terceiro SKU e a sincronização roda normalmente.
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"], [5, 3, 2]
    )
    resp = _sync(admin_catalog, MULTI_ID)
    assert "concluída" in resp.get_data(as_text=True)

    db.session.expire_all()
    _, rows = _origin_rows(db)
    assert resolve_rows(rows, f"{MULTI_ID}-1").origin == "brazil"
    assert resolve_rows(rows, f"{MULTI_ID}-2").origin == "brazil"
    # SKU nascido DEPOIS da confirmação não herda a origem BR.
    assert resolve_rows(rows, f"{MULTI_ID}-3").origin is None
    assert resolve_rows(rows, f"{MULTI_ID}-3").known is False


def test_reconfirmar_cobertura_inclui_sku_novo(admin_catalog, db):
    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)

    supplier_product = _supplier_product_from_db(db, MULTI_ID)
    record_manual_confirmation(
        supplier_product_id=supplier_product.id,
        origin="brazil",
        evidence_ref="ORIGEM-COB-002",
        covers_all=True,
    )
    db.session.commit()

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(
        ["27.59", "39.09", "46.60"], [5, 3, 2]
    )
    _sync(admin_catalog, MULTI_ID)
    db.session.expire_all()

    # Reconfirmar redeclara o escopo no MOMENTO da escrita: o SKU novo
    # passa a ser coberto junto com os antigos.
    supplier_product = _supplier_product_from_db(db, MULTI_ID)
    row = record_manual_confirmation(
        supplier_product_id=supplier_product.id,
        origin="brazil",
        evidence_ref="ORIGEM-COB-003",
        covers_all=True,
    )
    db.session.commit()
    assert covered_supplier_skus(row) == {
        f"{MULTI_ID}-1",
        f"{MULTI_ID}-2",
        f"{MULTI_ID}-3",
    }

    _, rows = _origin_rows(db)
    assert resolve_rows(rows, f"{MULTI_ID}-3").origin == "brazil"


def test_limpar_confirmacao_remove_cobertura(admin_catalog, db):
    from loja.services.shipping_origin import clear_manual_confirmation

    FakeAliExpressClient.details[MULTI_ID] = make_multi_detail(*TWO_SKUS)
    _publish(admin_catalog, MULTI_ID)
    _origem(admin_catalog, product_id=MULTI_ID, prefix="publicado")

    supplier_product = _supplier_product_from_db(db, MULTI_ID)
    _, rows = _origin_rows(db)
    assert resolve_rows(rows, f"{MULTI_ID}-1").origin == "brazil"

    assert clear_manual_confirmation(
        supplier_product_id=supplier_product.id, covers_all=True
    )
    db.session.commit()

    _, rows = _origin_rows(db)
    assert rows == [] or resolve_rows(rows, f"{MULTI_ID}-1").origin is None
