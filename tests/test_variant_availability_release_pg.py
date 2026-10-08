"""Disponibilidade comercial x restituição de reservas em PostgreSQL real.

Prova no dialeto com `SELECT ... FOR UPDATE` de verdade que:

1. a liberação por expiração devolve o estoque à variante DESABILITADA sem
   entrar no agregado `Product.stock` (soma canônica das ATIVAS), com
   encerramento idempotente e reativação incorporando o devolvido;
2. a restituição compartilhada (`release_reserved_stock`) tem o mesmo
   comportamento para variante ativa (agregado cresce) e desabilitada;
3. a corrida entre alternância de disponibilidade, liberação por expiração
   e checkout termina sem deadlock (mesma ordem de locks Product ->
   ProductVariant nos três caminhos) e com o invariante
   `product.stock == soma das ativas` intacto, sem ultrapassar os espelhos
   do fornecedor.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_avail_release_* e nunca toca public ou produção.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from datetime import timedelta
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

EXTERNAL_ID = "2000000000899"


@pytest.fixture
def pg_app(monkeypatch):
    schema = "pollo_test_avail_release_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "avail-release-pg")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {
                "options": "-csearch_path=%s -cstatement_timeout=20000" % schema
            },
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
    """Produto com 2 variantes e 2 pedidos vencidos (um por variante).

    Variante 1: espelho 5, local 3 (2 reservados em O1). Variante 2: espelho
    3, local 2 (1 reservado em O2). Agregado = 3 + 2 = 5.
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

    def _order(user, product, variant, quantity):
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
            customer_name="Cliente PG",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua PG",
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
                quantity=quantity,
                unit_price=Decimal("71.43"),
                line_total=Decimal("71.43") * quantity,
            )
        )
        return order

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Avail Release",
            email="avail-release-%s@pg.test" % uuid.uuid4().hex,
            is_admin=True,
        )
        user.set_password("senha123")

        supplier = Supplier(
            id=uuid.uuid4(),
            name="Fornecedor disponibilidade",
            channel_type="aliexpress",
            seller_id="SELLER_AVAIL",
        )
        db.session.add(supplier)
        db.session.flush()

        product = Product(
            id=uuid.uuid4(),
            name="Vestido disponibilidade",
            description="Produto sintético para disponibilidade x restituição.",
            image1="https://exemplo.com/avail.png",
            category="feminino",
            price=Decimal("142.86"),
            target_margin_percent=Decimal("30"),
            currency="BRL",
            source="aliexpress",
            external_id=EXTERNAL_ID,
            is_draft=False,
            is_active=True,
            pricing_status="priced",
            stock=5,
        )
        db.session.add(product)
        db.session.flush()

        link = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id="SP_AVAIL",
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

        variant1 = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=link.id,
            external_sku_id="%s-1" % EXTERNAL_ID,
            supplier_sku_id="%s-1" % EXTERNAL_ID,
            size="M",
            color="Vermelho",
            stock=3,
            supplier_stock=5,
            is_active=True,
        )
        variant2 = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=link.id,
            external_sku_id="%s-2" % EXTERNAL_ID,
            supplier_sku_id="%s-2" % EXTERNAL_ID,
            size="G",
            color="Azul",
            stock=2,
            supplier_stock=3,
            is_active=True,
        )
        db.session.add_all([variant1, variant2])

        order1 = _order(user, product, variant1, 2)
        order2 = _order(user, product, variant2, 1)
        db.session.add_all([user, order1, order2])
        db.session.commit()
        return {
            "product_id": product.id,
            "variant1_id": variant1.id,
            "variant2_id": variant2.id,
            "order1_id": order1.id,
            "order2_id": order2.id,
        }


def _state(app, ids):
    from loja import db
    from loja.modules.models import Product, ProductVariant

    with app.app_context():
        product = db.session.get(Product, ids["product_id"])
        v1 = db.session.get(ProductVariant, ids["variant1_id"])
        v2 = db.session.get(ProductVariant, ids["variant2_id"])
        return (
            int(product.stock),
            (int(v1.stock), bool(v1.is_active)),
            (int(v2.stock), bool(v2.is_active)),
        )


def test_expiracao_devolve_a_variante_sem_inflar_agregado(pg_app):
    """Ciclo em PG: desabilitar → liberar → repetir → reabilitar."""
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )
    from loja.services.variant_availability import set_variant_availability

    ids = _materialize(pg_app)

    with pg_app.app_context():
        product = db.session.get(Product, ids["product_id"])
        set_variant_availability(
            product=product,
            variant_id=ids["variant1_id"],
            available=False,
        )
        db.session.commit()
    assert _state(pg_app, ids) == (2, (3, False), (2, True))

    with pg_app.app_context():
        result = release_expired_stock_reservations(source="test")
        assert (result.released, result.skipped) == (2, 0)

    # O1 devolveu 2 à variante 1 (teto 5 − 0) sem tocar o agregado;
    # O2 devolveu 1 à variante 2 ATIVA (agregado cresce).
    assert _state(pg_app, ids) == (3, (5, False), (3, True))
    with pg_app.app_context():
        closed = db.session.get(Order, ids["order1_id"])
        assert closed.stock_reserved is False and closed.status == "expired"

    # Repetir: nada elegível, nada muda.
    with pg_app.app_context():
        repeat = release_expired_stock_reservations(source="test")
        assert (repeat.released, repeat.skipped) == (0, 0)
    assert _state(pg_app, ids) == (3, (5, False), (3, True))

    # Reabilitar incorpora o devolvido: 5 + 3 = 8.
    with pg_app.app_context():
        product = db.session.get(Product, ids["product_id"])
        set_variant_availability(
            product=product,
            variant_id=ids["variant1_id"],
            available=True,
        )
        db.session.commit()
    assert _state(pg_app, ids) == (8, (5, True), (3, True))


def test_restituicao_compartilhada_respeita_atividade(pg_app):
    """Via compartilhada: ativa soma no agregado, desabilitada não."""
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import release_reserved_stock
    from loja.services.variant_availability import set_variant_availability

    ids = _materialize(pg_app)

    with pg_app.app_context():
        product = db.session.get(Product, ids["product_id"])
        set_variant_availability(
            product=product,
            variant_id=ids["variant1_id"],
            available=False,
        )
        db.session.commit()

        release_reserved_stock(db.session.get(Order, ids["order1_id"]))
        release_reserved_stock(db.session.get(Order, ids["order2_id"]))
        db.session.commit()

    # Variante 1 (desabilitada): 3 → 5, agregado parado em 2.
    # Variante 2 (ativa): 2 → 3, agregado 2 → 3.
    assert _state(pg_app, ids) == (3, (5, False), (3, True))


def test_corrida_disponibilidade_release_checkout_sem_deadlock(pg_app):
    """Alternância x liberação x checkout: sem deadlock, invariante intacto."""
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )
    from loja.services.variant_availability import set_variant_availability
    from loja.views.payments_routes import _reserve_stock

    ids = _materialize(pg_app)
    errors = []

    def toggler():
        with pg_app.app_context():
            try:
                for i in range(8):
                    product = db.session.get(Product, ids["product_id"])
                    set_variant_availability(
                        product=product,
                        variant_id=ids["variant1_id"],
                        available=(i % 2 == 1),
                    )
                    db.session.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                db.session.remove()

    def releaser():
        with pg_app.app_context():
            try:
                for _ in range(8):
                    release_expired_stock_reservations(source="race")
            except Exception as exc:
                errors.append(exc)
            finally:
                db.session.remove()

    def buyer():
        with pg_app.app_context():
            try:
                product = db.session.get(Product, ids["product_id"])
                for _ in range(8):
                    try:
                        _reserve_stock(
                            [
                                {
                                    "product_id": ids["product_id"],
                                    "product_name": product.name,
                                    "variant_id": ids["variant2_id"],
                                    "quantity": 1,
                                    "unit_price": Decimal("71.43"),
                                }
                            ]
                        )
                        db.session.commit()
                    except ValueError:
                        db.session.rollback()
            except Exception as exc:
                errors.append(exc)
            finally:
                db.session.remove()

    threads = [
        threading.Thread(target=toggler),
        threading.Thread(target=releaser),
        threading.Thread(target=buyer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []

    with pg_app.app_context():
        assert not (
            db.session.query(Order.id)
            .filter(Order.stock_reserved.is_(True))
            .first()
        )
    aggregate, v1, v2 = _state(pg_app, ids)
    # Variante 1 termina ativa (última iteração i=7) com o total restituído.
    assert v1 == (5, True)
    # Invariante canônico vale em qualquer ordem de serialização, e nenhuma
    # variante ultrapassa o espelho do fornecedor.
    assert aggregate == (v1[0] if v1[1] else 0) + (v2[0] if v2[1] else 0)
    assert 0 <= v1[0] <= 5
    assert 0 <= v2[0] <= 3
