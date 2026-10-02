"""Concorrência permanente (item 1.4): webhook x liberação x aprovação admin.

Corridas ainda não cobertas pela suíte, nas duas direções de quem vence o
lock de linha do pedido:

1. webhook aprovado x ``release_single_expired_stock_reservation``;
2. aprovação manual_review x webhook aprovado tardio.

Somente banco descartável loopback com nome pollo*. Cada teste usa schema
pollo_test_pay_race_* e nunca toca public ou produção.
"""

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


@pytest.fixture
def pg_app(monkeypatch):
    schema = "pollo_test_pay_race_%s" % secrets.token_hex(4)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "%s"' % schema))

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "pay-race-pg")
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

    from loja import create_app, db

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()

    yield app

    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema))
    engine.dispose()


def _materialize(app, *, status="pending_payment"):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product, User
    from loja.utils import utcnow

    with app.app_context():
        user = User(
            id=uuid.uuid4(),
            name="PG Pay Race",
            email="pay-race-%s@pg.test" % uuid.uuid4().hex,
            is_admin=True,
        )
        user.set_password("senha123")
        product = Product(
            id=uuid.uuid4(),
            name="Produto PG corrida",
            description="sintético",
            price=Decimal("20.00"),
            stock=4,
            source="manual",
            is_draft=True,
            is_active=False,
        )
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status=status,
            subtotal=Decimal("20.00"),
            shipping=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("30.00"),
            stock_reserved=True,
            customer_name="Cliente sintético",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua sintética",
            number="10",
            neighborhood="Centro",
            city="São Paulo",
            state="SP",
            created_at=utcnow() - timedelta(hours=25),
            mp_preference_expires_at=utcnow() - timedelta(hours=1),
            manual_review_reason=(
                "corrida sintética" if status == "manual_review" else None
            ),
        )
        order.items.append(
            OrderItem(
                id=uuid.uuid4(),
                product_id=product.id,
                product_name=product.name,
                selected_size="M",
                quantity=1,
                unit_price=Decimal("20.00"),
                line_total=Decimal("20.00"),
            )
        )
        db.session.add_all([user, product, order])
        db.session.commit()
        return user.id, order.id, product.id


def _approved_payload(payment_id, order_id):
    return {
        "id": payment_id,
        "external_reference": str(order_id),
        "transaction_amount": "30.00",
        "currency_id": "BRL",
        "status": "approved",
        "status_detail": "accredited",
    }


def test_item_1_4_webhook_lock_beats_single_release(pg_app, monkeypatch):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )
    import loja.views.payments_routes as pr

    user_id, order_id, product_id = _materialize(pg_app)
    webhook_holding_lock = threading.Event()
    allow_webhook = threading.Event()
    release_finished = threading.Event()
    release_results = []
    errors = []
    original_map = pr.mp_status_to_order_status

    def paused_map(mp_status):
        # O pedido já está travado com FOR UPDATE neste ponto.
        webhook_holding_lock.set()
        assert allow_webhook.wait(10)
        return original_map(mp_status)

    monkeypatch.setattr(pr, "mp_status_to_order_status", paused_map)
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _approved_payload(pid, order_id),
    )

    def webhook_worker():
        try:
            with pg_app.app_context():
                pr._sync_payment("race-approved-1")
        except Exception as exc:
            errors.append(exc)

    def release_worker():
        try:
            with pg_app.app_context():
                release_results.append(
                    release_single_expired_stock_reservation(
                        order_id, source="pg_test"
                    )
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            release_finished.set()

    webhook = threading.Thread(target=webhook_worker)
    webhook.start()
    assert webhook_holding_lock.wait(10)

    release = threading.Thread(target=release_worker)
    release.start()
    # Sob o lock do webhook a liberação single não consegue terminar.
    assert not release_finished.wait(0.3)

    allow_webhook.set()
    webhook.join(15)
    release.join(15)
    assert not webhook.is_alive()
    assert not release.is_alive()
    assert errors == []
    # Pago vence: a liberação revalida e recusa (não é mais elegível).
    assert release_results == [False]

    with pg_app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        assert order.status == "paid"
        assert order.stock_reserved is True
        assert product.stock == 4


def test_item_1_4_single_release_then_late_webhook_repays_once(
    pg_app, monkeypatch
):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )
    import loja.views.payments_routes as pr
    import loja.services.stock_reservations as reservations

    user_id, order_id, product_id = _materialize(pg_app)
    release_holding_lock = threading.Event()
    allow_release = threading.Event()
    webhook_finished = threading.Event()
    release_results = []
    webhook_results = []
    errors = []
    original_release = reservations._release_stock_locked

    def paused_release(order):
        # O pedido já está travado com FOR UPDATE neste ponto.
        release_holding_lock.set()
        assert allow_release.wait(10)
        return original_release(order)

    monkeypatch.setattr(reservations, "_release_stock_locked", paused_release)
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _approved_payload(pid, order_id),
    )

    def release_worker():
        try:
            with pg_app.app_context():
                release_results.append(
                    release_single_expired_stock_reservation(
                        order_id, source="pg_test"
                    )
                )
        except Exception as exc:
            errors.append(exc)

    def webhook_worker():
        try:
            with pg_app.app_context():
                synced = pr._sync_payment("race-approved-2")
                webhook_results.append(synced.status if synced else None)
        except Exception as exc:
            errors.append(exc)
        finally:
            webhook_finished.set()

    release = threading.Thread(target=release_worker)
    release.start()
    assert release_holding_lock.wait(10)

    webhook = threading.Thread(target=webhook_worker)
    webhook.start()
    # Sob o lock da liberação o webhook não consegue terminar.
    assert not webhook_finished.wait(0.3)

    allow_release.set()
    release.join(15)
    webhook.join(15)
    assert not release.is_alive()
    assert not webhook.is_alive()
    assert errors == []
    # Liberação vence: vira expired; o webhook tardio reconstrói pagando.
    assert release_results == [True]
    assert webhook_results == ["paid"]

    with pg_app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        assert order.status == "paid"
        assert order.stock_reserved is True
        assert product.stock == 4


def test_item_1_4_approve_lock_beats_webhook_reensures(pg_app, monkeypatch):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    import loja.views.payments_routes as pr
    import loja.services.order_promotion as order_promotion

    user_id, order_id, product_id = _materialize(
        pg_app, status="manual_review"
    )
    approve_in_revalidate = threading.Event()
    allow_approve = threading.Event()
    webhook_finished = threading.Event()
    approve_results = []
    webhook_results = []
    errors = []
    original_revalidate = order_promotion.revalidate_and_promote_paid

    def paused_revalidate(order):
        # A aprovação já travou o pedido com FOR UPDATE neste ponto.
        approve_in_revalidate.set()
        assert allow_approve.wait(10)
        return original_revalidate(order)

    monkeypatch.setattr(
        order_promotion, "revalidate_and_promote_paid", paused_revalidate
    )
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _approved_payload(pid, order_id),
    )

    def approve_worker():
        try:
            with pg_app.app_context():
                result = order_promotion.approve_manual_review(
                    order_id, admin_user_id=user_id, note="corrida pg"
                )
                approve_results.append(result.outcome)
        except Exception as exc:
            errors.append(exc)

    def webhook_worker():
        try:
            with pg_app.app_context():
                synced = pr._sync_payment("race-approved-3")
                webhook_results.append(synced.status if synced else None)
        except Exception as exc:
            errors.append(exc)
        finally:
            webhook_finished.set()

    approve = threading.Thread(target=approve_worker)
    approve.start()
    assert approve_in_revalidate.wait(10)

    webhook = threading.Thread(target=webhook_worker)
    webhook.start()
    # Sob o lock da aprovação o webhook não consegue terminar.
    assert not webhook_finished.wait(0.3)

    allow_approve.set()
    approve.join(15)
    webhook.join(15)
    assert not approve.is_alive()
    assert not webhook.is_alive()
    assert errors == []
    # Aprovação vence: paga com auditoria; webhook vira re-ensure idempotente.
    assert approve_results == ["changed"]
    assert webhook_results == ["paid"]

    with pg_app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        assert order.status == "paid"
        assert order.stock_reserved is True
        assert order.manual_review_decision == "approved"
        assert order.manual_review_decided_by_user_id == user_id
        assert order.manual_review_decided_at is not None
        assert product.stock == 4


def test_item_1_4_webhook_lock_approve_completes_after(pg_app, monkeypatch):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product
    import loja.views.payments_routes as pr
    import loja.services.order_promotion as order_promotion

    user_id, order_id, product_id = _materialize(
        pg_app, status="manual_review"
    )
    webhook_holding_lock = threading.Event()
    allow_webhook = threading.Event()
    approve_attempted = threading.Event()
    approve_results = []
    webhook_results = []
    errors = []
    original_map = pr.mp_status_to_order_status
    original_locked_order = order_promotion._locked_order

    def paused_map(mp_status):
        # O pedido já está travado com FOR UPDATE neste ponto.
        webhook_holding_lock.set()
        assert allow_webhook.wait(10)
        return original_map(mp_status)

    def signaling_locked_order(order_id_arg):
        approve_attempted.set()
        return original_locked_order(order_id_arg)

    monkeypatch.setattr(pr, "mp_status_to_order_status", paused_map)
    monkeypatch.setattr(order_promotion, "_locked_order", signaling_locked_order)
    monkeypatch.setattr(
        pr,
        "get_payment",
        lambda pid: _approved_payload(pid, order_id),
    )

    def webhook_worker():
        try:
            with pg_app.app_context():
                synced = pr._sync_payment("race-approved-4")
                webhook_results.append(synced.status if synced else None)
        except Exception as exc:
            errors.append(exc)

    def approve_worker():
        try:
            with pg_app.app_context():
                result = order_promotion.approve_manual_review(
                    order_id, admin_user_id=user_id, note="corrida pg"
                )
                approve_results.append(result.outcome)
        except Exception as exc:
            errors.append(exc)

    webhook = threading.Thread(target=webhook_worker)
    webhook.start()
    assert webhook_holding_lock.wait(10)

    approve = threading.Thread(target=approve_worker)
    approve.start()
    # A aprovação espera o mesmo lock segurado pelo webhook.
    assert approve_attempted.wait(10)

    allow_webhook.set()
    webhook.join(15)
    approve.join(15)
    assert not webhook.is_alive()
    assert not approve.is_alive()
    assert errors == []
    # Webhook não reanima manual_review; a aprovação conclui depois, sozinha.
    assert webhook_results == ["manual_review"]
    assert approve_results == ["changed"]

    with pg_app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        assert order.status == "paid"
        assert order.stock_reserved is True
        assert order.manual_review_decision == "approved"
        assert order.manual_review_decided_by_user_id == user_id
        assert product.stock == 4
