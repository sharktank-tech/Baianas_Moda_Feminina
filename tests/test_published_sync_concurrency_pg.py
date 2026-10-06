"""Concorrência do ponto 4 da revisão PR #123: sync x checkout x release.

A sincronização técnica do produto publicado recalcula estoque ABSOLUTO sob
lock (`SELECT ... FOR UPDATE` em Product -> ProductVariant, a mesma ordem do
checkout) e subtrai a contabilidade de reservas. Estas corridas provam em
PostgreSQL real que:

1. o checkout não lê/escreve o produto enquanto a sincronização segura os
   locks (nada de lost update: a reserva feita depois nunca é engolida);
2. a liberação de reserva vencida serializa com a sincronização — o
   resultado final é o mesmo nas duas ordens (nunca perde nem duplica
   estoque);
3. o caminho inverso: a sincronização ESPERA o checkout, recarrega os
   objetos já carregados sob o lock (`populate_existing`) e aplica o
   absoluto por cima dos valores frescos (nunca o snapshot anterior).

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_sync_race_* e nunca toca public ou produção.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    _url = make_url(DATABASE_URL) if DATABASE_URL else None
    IS_SAFE_POSTGRES = bool(
        _url
        and _url.get_backend_name() == "postgresql"
        and _url.host in {"127.0.0.1", "localhost", "::1"}
        and str(_url.database or "").startswith("pollo")
    )
except Exception:
    IS_SAFE_POSTGRES = False

pytestmark = pytest.mark.skipif(
    not IS_SAFE_POSTGRES,
    reason="requer PostgreSQL descartável loopback pollo*",
)

EXTERNAL_ID = "2000000000888"


@pytest.fixture
def pg_app(monkeypatch):
    schema = "pollo_test_sync_race_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "sync-race-pg")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {"options": "-csearch_path=%s" % schema},
        },
    )
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", schema)

    from loja import create_app

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        from loja import db

        db.create_all()

    yield app

    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _materialize(app):
    """Produto publicado + pedido pendente vencido com 2 unidades reservadas.

    Estoque local = 3 (fornecedor 5 − 2 já debitadas); o pedido está
    ``pending_payment`` com ``mp_preference_expires_at`` no passado para
    ser elegível à liberação.
    """
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import (
        Product,
        ProductVariant,
        Supplier,
        SupplierProduct,
        User,
    )
    from loja.utils import utcnow
    from datetime import timedelta

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Sync Race",
            email="sync-race-%s@pg.test" % uuid.uuid4().hex,
            is_admin=True,
        )
        user.set_password("senha123")

        supplier = Supplier(
            id=uuid.uuid4(),
            name="Fornecedor corrida",
            channel_type="aliexpress",
            seller_id="SELLER_RACE",
        )
        db.session.add(supplier)
        db.session.flush()

        product = Product(
            id=uuid.uuid4(),
            name="Camiseta corrida",
            description="Produto sintético para corrida sync x checkout.",
            image1="https://exemplo.com/race.png",
            category="masculino",
            price=Decimal("142.86"),
            target_margin_percent=Decimal("30"),
            currency="BRL",
            source="aliexpress",
            external_id=EXTERNAL_ID,
            is_draft=False,
            is_active=True,
            pricing_status="priced",
            stock=3,
        )
        db.session.add(product)
        db.session.flush()

        link = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id="SP_RACE",
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
            external_sku_id="%s-1" % EXTERNAL_ID,
            supplier_sku_id="%s-1" % EXTERNAL_ID,
            size="M",
            color="Vermelho",
            stock=3,
            is_active=True,
        )
        db.session.add(variant)

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
            customer_name="Cliente corrida",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua corrida",
            number="10",
            neighborhood="Centro",
            city="São Paulo",
            state="SP",
            created_at=utcnow() - timedelta(hours=25),
            mp_preference_expires_at=utcnow() - timedelta(hours=1),
        )
        order.items.append(
            OrderItem(
                id=uuid.uuid4(),
                product_id=product.id,
                variant_id=variant.id,
                product_name=product.name,
                selected_size="M",
                quantity=2,
                unit_price=Decimal("71.43"),
                line_total=Decimal("142.86"),
            )
        )
        db.session.add_all([user, order])
        db.session.commit()
        return {
            "user_id": user.id,
            "product_id": product.id,
            "link_id": link.id,
            "variant_id": variant.id,
            "order_id": order.id,
        }


def _detail(supplier_stock=5):
    from loja.integrations.aliexpress.models import (
        AliExpressProductDetail,
        AliExpressProductSku,
    )

    return AliExpressProductDetail(
        product_id=EXTERNAL_ID,
        title="Camiseta corrida",
        description="Sincronização técnica em corrida.",
        skus=[
            AliExpressProductSku(
                sku_id="%s-1" % EXTERNAL_ID,
                sale_price=Decimal("80.00"),
                original_price=Decimal("80.00"),
                currency="BRL",
                available_stock=supplier_stock,
                attributes=[
                    {"name": "Cor", "value": "Vermelho"},
                    {"name": "Tamanho", "value": "M"},
                ],
            )
        ],
        available_stock=supplier_stock,
        sale_price=Decimal("80.00"),
        original_price=Decimal("80.00"),
        currency="BRL",
    )


def _snapshot(app, variant_id):
    from loja import db
    from loja.modules.models import ProductVariant

    with app.app_context():
        variant = db.session.get(ProductVariant, variant_id)
        return int(variant.stock), bool(variant.is_active)


def test_sync_segura_locks_e_checkout_espera_sem_lost_update(
    pg_app, monkeypatch
):
    """Checkout serializa atrás da sync: a reserva nunca é engolida."""
    from loja import db
    from loja.integrations.aliexpress import published_sync as ps
    from loja.modules.models import Product, ProductVariant, SupplierProduct
    from loja.services.purchase_controls import purchase_block_codes
    from loja.views.payments_routes import _reserve_stock

    ids = _materialize(pg_app)

    sync_holding_lock = threading.Event()
    allow_sync = threading.Event()
    checkout_finished = threading.Event()
    checkout_errors = []
    sync_results = []
    errors = []
    original_reserved = ps.reserved_quantities_by_variant

    def paused_reserved(variant_ids):
        # Neste ponto a sync já travou Product e ProductVariant.
        sync_holding_lock.set()
        assert allow_sync.wait(10)
        return original_reserved(variant_ids)

    monkeypatch.setattr(ps, "reserved_quantities_by_variant", paused_reserved)

    def sync_worker():
        try:
            with pg_app.app_context():
                result = ps.sync_published_product(
                    detail=_detail(5),
                    product=db.session.get(Product, ids["product_id"]),
                    supplier_product=db.session.get(
                        SupplierProduct, ids["link_id"]
                    ),
                )
                db.session.commit()
                sync_results.append(result)
        except Exception as exc:
            errors.append(exc)

    def checkout_worker():
        try:
            with pg_app.app_context():
                product = db.session.get(Product, ids["product_id"])
                variant = db.session.get(ProductVariant, ids["variant_id"])
                assert purchase_block_codes(product, variant) == []
                _reserve_stock(
                    [
                        {
                            "product_id": ids["product_id"],
                            "product_name": product.name,
                            "variant_id": ids["variant_id"],
                            "selected_size": "M",
                            "quantity": 1,
                            "unit_price": product.price,
                            "line_total": product.price,
                        }
                    ]
                )
                db.session.commit()
        except Exception as exc:
            checkout_errors.append(exc)
        finally:
            checkout_finished.set()

    sync_thread = threading.Thread(target=sync_worker)
    sync_thread.start()
    assert sync_holding_lock.wait(10)

    checkout_thread = threading.Thread(target=checkout_worker)
    checkout_thread.start()
    # Sob os locks da sync o checkout não termina.
    assert not checkout_finished.wait(0.5)

    allow_sync.set()
    sync_thread.join(15)
    checkout_thread.join(15)
    assert not sync_thread.is_alive() and not checkout_thread.is_alive()
    assert errors == [] and checkout_errors == []

    # Sync gravou o absoluto (5 − 2 reservados = 3) e o checkout subsequente
    # debitou 1: a reserva nova não foi engolida pelo write absoluto.
    assert sync_results and sync_results[0].stock_after == 3
    stock, active = _snapshot(pg_app, ids["variant_id"])
    assert stock == 2
    assert active is True


def test_sync_serializa_com_release_de_reserva_vencida(pg_app, monkeypatch):
    """Release x sync: mesma ordem final (5) nas duas ordens de execução."""
    from loja import db
    from loja.integrations.aliexpress import published_sync as ps
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product, SupplierProduct
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    ids = _materialize(pg_app)

    sync_holding_lock = threading.Event()
    allow_sync = threading.Event()
    release_finished = threading.Event()
    release_results = []
    sync_results = []
    errors = []
    original_reserved = ps.reserved_quantities_by_variant

    def paused_reserved(variant_ids):
        sync_holding_lock.set()
        assert allow_sync.wait(10)
        return original_reserved(variant_ids)

    monkeypatch.setattr(ps, "reserved_quantities_by_variant", paused_reserved)

    def sync_worker():
        try:
            with pg_app.app_context():
                result = ps.sync_published_product(
                    detail=_detail(5),
                    product=db.session.get(Product, ids["product_id"]),
                    supplier_product=db.session.get(
                        SupplierProduct, ids["link_id"]
                    ),
                )
                db.session.commit()
                sync_results.append(result)
        except Exception as exc:
            errors.append(exc)

    def release_worker():
        try:
            with pg_app.app_context():
                release_results.append(
                    release_single_expired_stock_reservation(
                        ids["order_id"], source="pg_test"
                    )
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            release_finished.set()

    sync_thread = threading.Thread(target=sync_worker)
    sync_thread.start()
    assert sync_holding_lock.wait(10)

    release_thread = threading.Thread(target=release_worker)
    release_thread.start()
    # A liberação segura o pedido e espera os locks do produto/variante.
    assert not release_finished.wait(0.5)

    allow_sync.set()
    sync_thread.join(15)
    release_thread.join(15)
    assert not sync_thread.is_alive() and not release_thread.is_alive()
    assert errors == []
    assert release_results == [True]

    # Sync (5 − 2 reservados = 3) e depois release (+2): estoque volta ao
    # total do fornecedor, sem perda nem duplicação de baixa.
    assert sync_results and sync_results[0].stock_after == 3
    stock, active = _snapshot(pg_app, ids["variant_id"])
    assert stock == 5
    assert active is True

    with pg_app.app_context():
        order = db.session.get(Order, ids["order_id"])
        assert order.status == "expired"
        assert order.stock_reserved is False


def test_release_antes_da_sync_mantem_mesmo_resultado(pg_app):
    """Controle: liberação primeiro, sync depois — mesmo total (5)."""
    from loja import db
    from loja.integrations.aliexpress.published_sync import (
        sync_published_product,
    )
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product, SupplierProduct
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    ids = _materialize(pg_app)

    with pg_app.app_context():
        assert release_single_expired_stock_reservation(
            ids["order_id"], source="pg_test"
        )
        result = sync_published_product(
            detail=_detail(5),
            product=db.session.get(Product, ids["product_id"]),
            supplier_product=db.session.get(SupplierProduct, ids["link_id"]),
        )
        db.session.commit()

    assert result.stock_after == 5
    stock, active = _snapshot(pg_app, ids["variant_id"])
    assert stock == 5 and active is True

    with pg_app.app_context():
        order = db.session.get(Order, ids["order_id"])
        assert order.status == "expired"


def test_sync_aguarda_checkout_e_recarrega_registros(pg_app, monkeypatch):
    """O caminho inverso: a sync ESPERA o checkout e recarrega os objetos.

    Ordem da corrida: a sessão da sync carrega produto/variante (snapshot
    3/3); um checkout concorrente debita 1 unidade e COMMITA; só então a
    sincronização adquire os locks — bloqueada atrás do checkout — e, sob
    o lock, RECARREGA as instâncias já presentes no identity map
    (`populate_existing`) antes de decidir.

    Sem o recarregamento, `stock_before` sairia velho (3) e o comparador
    veria "3 == 3", não gravando nada: o banco ficaria com a variante na
    2 do checkout e o produto na 3 calculada a partir do snapshot — fora
    de sincronia. Com o recarregamento, o absoluto do fornecedor (5 − 2
    reservadas = 3) é aplicado por cima dos valores frescos.
    """
    from loja import db
    from loja.integrations.aliexpress import published_sync as ps
    from loja.modules.models import Product, ProductVariant, SupplierProduct
    from loja.views import payments_routes
    from loja.views.payments_routes import _reserve_stock

    ids = _materialize(pg_app)

    checkout_holding_lock = threading.Event()
    allow_checkout = threading.Event()
    checkout_committed = threading.Event()
    sync_reached_locks = threading.Event()
    checkout_errors = []
    errors = []
    results = []

    original_allowed = payments_routes.assert_purchase_allowed

    def paused_allowed(product, variant=None, *, product_name=None):
        # Neste ponto o checkout já segura Product + ProductVariant.
        checkout_holding_lock.set()
        assert allow_checkout.wait(10)
        return original_allowed(product, variant, product_name=product_name)

    monkeypatch.setattr(
        payments_routes, "assert_purchase_allowed", paused_allowed
    )

    def checkout_worker():
        try:
            with pg_app.app_context():
                product = db.session.get(Product, ids["product_id"])
                _reserve_stock(
                    [
                        {
                            "product_id": ids["product_id"],
                            "product_name": product.name,
                            "variant_id": ids["variant_id"],
                            "selected_size": "M",
                            "quantity": 1,
                        }
                    ]
                )
                # Mantém os locks ocupados enquanto a sync chega a eles.
                time.sleep(0.1)
                db.session.commit()
        except Exception as exc:
            checkout_errors.append(exc)
        finally:
            checkout_committed.set()

    def releaser_worker():
        # Só solta o checkout quando a sync já chegou aos locks.
        assert sync_reached_locks.wait(10)
        allow_checkout.set()

    checkout_thread = threading.Thread(target=checkout_worker)
    releaser_thread = threading.Thread(target=releaser_worker)

    with pg_app.app_context():
        # Snapshot da sessão da sync ANTES do checkout commitar.
        product = db.session.get(Product, ids["product_id"])
        variant = db.session.get(ProductVariant, ids["variant_id"])
        assert int(product.stock) == 3 and int(variant.stock) == 3

        checkout_thread.start()
        assert checkout_holding_lock.wait(10)

        original_locked = ps._locked_variants

        def signal_locked(locked_product, supplier_product):
            # Chegou aos locks COM o checkout ainda dono deles.
            assert not checkout_committed.is_set()
            sync_reached_locks.set()
            return original_locked(locked_product, supplier_product)

        monkeypatch.setattr(ps, "_locked_variants", signal_locked)
        releaser_thread.start()

        try:
            results.append(
                ps.sync_published_product(
                    detail=_detail(5),
                    product=product,
                    supplier_product=db.session.get(
                        SupplierProduct, ids["link_id"]
                    ),
                )
            )
            db.session.commit()
        except Exception as exc:
            errors.append(exc)

    checkout_thread.join(15)
    releaser_thread.join(15)
    assert not checkout_thread.is_alive() and not releaser_thread.is_alive()
    assert errors == [] and checkout_errors == []
    assert checkout_committed.is_set()

    # Recarregado sob o lock: o checkout já debitou (2), não o 3 velho.
    assert results[0].stock_before == 2
    # Absoluto do fornecedor (5) − 2 reservadas = 3.
    assert results[0].stock_after == 3

    with pg_app.app_context():
        variant = db.session.get(ProductVariant, ids["variant_id"])
        product = db.session.get(Product, ids["product_id"])
        assert int(variant.stock) == 3
        assert int(variant.supplier_stock) == 5
        assert int(product.stock) == 3
