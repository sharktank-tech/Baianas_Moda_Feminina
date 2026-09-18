"""Testes dedicados do Payment Guard - Fase 8.1.0.

Dez cenarios que provam bloqueio fail-closed sem pagamento canonico aprovado.
Para cada bloqueio: contagens globais antes/depois nao aumentam, nenhum
AliExpressOrder pertence ao pedido testado, zero tentativas/ledger/remoto
e zero chamadas aos spies.
"""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from loja import db
from loja.integrations.aliexpress.credential_store import persist_credential
from loja.integrations.aliexpress.fulfillment import (
    ERROR_CODES,
    _validate_payment_guard,
    ensure_aliexpress_fulfillment,
)
from loja.modules.getway_mercado_pago.payment_models import OrderItem
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    AliExpressRemoteOrder,
    AliExpressSubmissionAttempt,
    Order,
    Payment,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)


# ---------------------------------------------------------------------------
# Helpers explicitos (somente este arquivo)
# ---------------------------------------------------------------------------

_ORDER_BASE = dict(
    checkout_type="product",
    subtotal=Decimal("29.90"),
    total=Decimal("29.90"),
    stock_reserved=False,
    customer_name="Test User",
    customer_email="test@test.com",
    customer_phone="11999999999",
    zip_code="01001000",
    street="Rua Test",
    number="1",
    complement="Apto 101",
    neighborhood="Centro",
    city="São Paulo",
    state="SP",
)


def make_order_without_payment(db, make_user, *, status="pending_payment"):
    """Pedido sem nenhum Payment vinculado."""
    user = make_user()
    order = Order(id=uuid4(), user_id=user.id, status=status, **_ORDER_BASE)
    db.session.add(order)
    db.session.commit()
    return order


def make_canonically_paid_order(db, make_user, *, total=Decimal("29.90")):
    """Pedido paid com Payment approved BRL vinculado e valor conferindo."""
    user = make_user()
    order = Order(
        id=uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="paid",
        subtotal=total,
        total=total,
        stock_reserved=False,
        customer_name="Test User",
        customer_email="test@test.com",
        customer_phone="11999999999",
        zip_code="01001000",
        street="Rua Test",
        number="1",
        complement="Apto 101",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        id=uuid4(),
        order_id=order.id,
        provider_payment_id=f"mp_test_{uuid4().hex[:12]}",
        status="approved",
        amount=total,
        currency="BRL",
    )
    db.session.add(payment)
    db.session.commit()
    return order


def make_valid_ali_item(db, order, product, variant):
    """OrderItem valido vinculado ao catalogo AliExpress."""
    item = OrderItem(
        id=uuid4(),
        order_id=order.id,
        product_id=product.id,
        variant_id=variant.id,
        product_name=product.name,
        quantity=1,
        unit_price=Decimal("29.90"),
        line_total=Decimal("29.90"),
    )
    db.session.add(item)
    db.session.commit()
    return item


class SpyRefresher:
    """Spy para o refresher OAuth (nao deve ser chamado nos bloqueios)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


class SpySubmitter:
    """Spy documental: fulfillment nao expoe submitter; registra uso indevido."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("submitter nao deve ser acionado pelo guard")


@pytest.fixture()
def fernet_key():
    return Fernet.generate_key().decode("utf-8")


@pytest.fixture()
def valid_credential_env(fernet_key, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_CREDENTIAL_KEY", fernet_key)


@pytest.fixture()
def ali_catalog(db):
    """Catalogo AliExpress valido: supplier + product + sp + variant."""
    supplier = Supplier(
        id=uuid4(),
        name="AliExpress Test",
        channel_type="aliexpress",
        seller_id="seller_123",
        default_currency="USD",
        is_sync_enabled=False,
    )
    db.session.add(supplier)
    db.session.flush()
    product = Product(
        id=uuid4(),
        name="Produto AliExpress Test",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="ext_123",
        is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.flush()
    sp = SupplierProduct(
        id=uuid4(),
        supplier_id=supplier.id,
        product_id=product.id,
        supplier_product_id="SP001",
        cost=Decimal("10.00"),
        currency="USD",
        is_active=True,
    )
    db.session.add(sp)
    db.session.flush()
    variant = ProductVariant(
        id=uuid4(),
        product_id=product.id,
        supplier_product_id=sp.id,
        supplier_sku_id="SKU001",
        sku=None,
        color="Red",
        size="M",
        attributes=[
            {
                "name": "Color",
                "value": "Red",
                "sku_property_id": "14",
                "property_value_id": "771",
            },
            {
                "name": "Size",
                "value": "M",
                "sku_property_id": "200000124",
                "property_value_id": "100013888",
            },
        ],
        stock=10,
        price=Decimal("29.90"),
        is_active=True,
    )
    db.session.add(variant)
    db.session.commit()
    return supplier, product, sp, variant


def snapshot_counts():
    """Contagens globais para provar zero efeito colateral."""
    return {
        "ali_orders": AliExpressOrder.query.count(),
        "ali_items": AliExpressOrderItem.query.count(),
        "attempts": AliExpressSubmissionAttempt.query.count(),
        "remotes": AliExpressRemoteOrder.query.count(),
    }


def assert_no_effects_for(order_id, before, refresher, submitter):
    """Contagens globais nao aumentam e nada pertence ao pedido testado."""
    after = snapshot_counts()
    assert after == before
    assert AliExpressOrder.query.filter_by(order_id=order_id).count() == 0
    own_ids = [
        row.id
        for row in AliExpressOrder.query.filter_by(order_id=order_id).all()
    ]
    assert own_ids == []
    assert (
        AliExpressSubmissionAttempt.query.join(
            AliExpressOrder,
            AliExpressSubmissionAttempt.aliexpress_order_id == AliExpressOrder.id,
        )
        .filter(AliExpressOrder.order_id == order_id)
        .count()
        == 0
    )
    assert (
        AliExpressRemoteOrder.query.join(
            AliExpressOrder,
            AliExpressRemoteOrder.aliexpress_order_id == AliExpressOrder.id,
        )
        .filter(AliExpressOrder.order_id == order_id)
        .count()
        == 0
    )
    assert refresher.calls == []
    assert submitter.calls == []


# ---------------------------------------------------------------------------
# Dez cenarios
# ---------------------------------------------------------------------------

def test_pending_payment_no_payment_blocks(db, make_user):
    order = make_order_without_payment(db, make_user, status="pending_payment")
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert _validate_payment_guard(order) == ERROR_CODES["PAYMENT_GUARD_NOT_PAID"]
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_pending_payment_with_pending_payment_record_blocks(db, make_user):
    order = make_order_without_payment(db, make_user, status="pending_payment")
    db.session.add(
        Payment(
            id=uuid4(),
            order_id=order.id,
            provider_payment_id=f"mp_test_{uuid4().hex[:12]}",
            status="pending",
            amount=order.total,
            currency="BRL",
        )
    )
    db.session.commit()
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    # Status != paid bloqueia antes da checagem de Payment (fail-closed).
    assert _validate_payment_guard(order) == ERROR_CODES["PAYMENT_GUARD_NOT_PAID"]
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_cancelled_order_without_approved_payment_blocks(db, make_user):
    order = make_order_without_payment(db, make_user, status="pending_payment")
    db.session.add(
        Payment(
            id=uuid4(),
            order_id=order.id,
            provider_payment_id=f"mp_test_{uuid4().hex[:12]}",
            status="rejected",
            amount=order.total,
            currency="BRL",
        )
    )
    order.status = "cancelled"
    db.session.commit()
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert _validate_payment_guard(order) == ERROR_CODES["PAYMENT_GUARD_NOT_PAID"]
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_paid_without_canonical_payment_blocks(db, make_user):
    order = make_canonically_paid_order(db, make_user)
    Payment.query.filter_by(order_id=order.id).delete()
    db.session.commit()
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(order)
        == ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]
    )
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_approved_payment_linked_to_other_order_blocks(db, make_user):
    paid_order = make_canonically_paid_order(db, make_user)
    other = make_order_without_payment(db, make_user, status="paid")
    Payment.query.filter_by(order_id=paid_order.id).update({"order_id": other.id})
    db.session.commit()
    assert Payment.query.filter_by(order_id=paid_order.id).count() == 0
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(paid_order)
        == ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]
    )
    assert ensure_aliexpress_fulfillment(paid_order, refresher=refresher) == []
    assert_no_effects_for(paid_order.id, before, refresher, submitter)


def test_order_in_manual_review_blocks(db, make_user):
    order = make_canonically_paid_order(db, make_user)
    order.status = "manual_review"
    order.manual_review_reason = "Valor/moeda divergente"
    db.session.commit()
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(order) == ERROR_CODES["PAYMENT_GUARD_MANUAL_REVIEW"]
    )
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_paid_with_value_mismatch_blocks(db, make_user):
    order = make_canonically_paid_order(db, make_user)
    Payment.query.filter_by(order_id=order.id).update(
        {"amount": order.total + Decimal("1.00")}
    )
    db.session.commit()
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(order) == ERROR_CODES["PAYMENT_GUARD_VALUE_MISMATCH"]
    )
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_paid_with_currency_mismatch_blocks(db, make_user):
    order = make_canonically_paid_order(db, make_user)
    Payment.query.filter_by(order_id=order.id).update({"currency": "USD"})
    db.session.commit()
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(order)
        == ERROR_CODES["PAYMENT_GUARD_CURRENCY_MISMATCH"]
    )
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_paid_with_approved_payment_allows_fulfillment(
    db, make_user, ali_catalog, valid_credential_env
):
    supplier, product, sp, variant = ali_catalog
    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok",
        expires_in=3600,
    )
    db.session.commit()
    order = make_canonically_paid_order(db, make_user)
    make_valid_ali_item(db, order, product, variant)
    refresher = SpyRefresher()
    assert _validate_payment_guard(order) is None
    results = ensure_aliexpress_fulfillment(order, refresher=refresher)
    assert len(results) == 1
    assert results[0].state == "ready"
    assert AliExpressOrder.query.filter_by(order_id=order.id).count() == 1
    assert refresher.calls == []


def test_preexisting_ali_order_does_not_affect_new_unpaid_order(
    db, make_user, ali_catalog, valid_credential_env
):
    """AliExpressOrder preexistente de outro pedido nao e alterada; nada e criado para o pedido bloqueado."""
    supplier, product, sp, variant = ali_catalog
    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok",
        expires_in=3600,
    )
    db.session.commit()

    other_order = make_canonically_paid_order(db, make_user)
    make_valid_ali_item(db, other_order, product, variant)
    created = ensure_aliexpress_fulfillment(other_order)
    assert len(created) == 1
    preexisting = AliExpressOrder.query.filter_by(order_id=other_order.id).one()
    preexisting_state = preexisting.state
    preexisting_attempts = preexisting.attempt_count
    preexisting_items = AliExpressOrderItem.query.filter_by(
        aliexpress_order_id=preexisting.id
    ).count()

    blocked = make_order_without_payment(db, make_user, status="paid")
    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(blocked)
        == ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]
    )
    assert ensure_aliexpress_fulfillment(blocked, refresher=refresher) == []
    assert_no_effects_for(blocked.id, before, refresher, submitter)

    db.session.refresh(preexisting)
    assert preexisting.state == preexisting_state
    assert preexisting.attempt_count == preexisting_attempts
    assert (
        AliExpressOrderItem.query.filter_by(aliexpress_order_id=preexisting.id).count()
        == preexisting_items
    )
    assert AliExpressOrder.query.filter_by(order_id=blocked.id).count() == 0


def test_approved_then_rejected_same_record_blocks(db, make_user):
    """Aprovado seguido de rejeitado NO MESMO registro bloqueia.

    Documenta o comportamento de _sync_payment: eventos posteriores do mesmo
    provider_payment_id (único) ATUALIZAM o registro em vez de criar outro;
    existe portanto UMA linha, agora com status rejeitado.
    """
    order = make_canonically_paid_order(db, make_user)
    payment = Payment.query.filter_by(order_id=order.id).one()
    original_id = payment.id
    payment.status = "rejected"
    db.session.commit()
    assert Payment.query.filter_by(order_id=order.id).count() == 1
    assert Payment.query.filter_by(order_id=order.id).one().id == original_id

    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert (
        _validate_payment_guard(order)
        == ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]
    )
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_approved_then_refunded_blocks(db, make_user):
    """Aprovado seguido de reembolso NO MESMO registro bloqueia.

    O modelo representa reembolso atualizando o registro (status refunded) e
    o pedido vai para terminal refunded; approved tardio nunca reverte
    (mesma regra de _sync_payment: pedido refunded/charged_back prevalece).
    """
    order = make_canonically_paid_order(db, make_user)
    payment = Payment.query.filter_by(order_id=order.id).one()
    payment.status = "refunded"
    order.status = "refunded"
    db.session.commit()
    assert Payment.query.filter_by(order_id=order.id).count() == 1

    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    assert _validate_payment_guard(order) == ERROR_CODES["PAYMENT_GUARD_NOT_PAID"]
    assert ensure_aliexpress_fulfillment(order, refresher=refresher) == []
    assert_no_effects_for(order.id, before, refresher, submitter)


def test_two_approved_payments_latest_wins_allows(
    db, make_user, ali_catalog, valid_credential_env
):
    """Dois payments aprovados: o evento vigente (mais recente) aprova."""
    supplier, product, sp, variant = ali_catalog
    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok",
        expires_in=3600,
    )
    db.session.commit()
    order = make_canonically_paid_order(db, make_user)
    make_valid_ali_item(db, order, product, variant)
    db.session.add(
        Payment(
            id=uuid4(),
            order_id=order.id,
            provider_payment_id=f"mp_test_{uuid4().hex[:12]}",
            status="approved",
            amount=order.total,
            currency="BRL",
        )
    )
    db.session.commit()
    assert Payment.query.filter_by(order_id=order.id).count() == 2

    refresher = SpyRefresher()
    assert _validate_payment_guard(order) is None
    results = ensure_aliexpress_fulfillment(order, refresher=refresher)
    assert len(results) == 1
    assert results[0].state == "ready"
    assert AliExpressOrder.query.filter_by(order_id=order.id).count() == 1
    assert refresher.calls == []


def test_created_at_tie_breaks_by_id_deterministically(db, make_user):
    """Empate em created_at: desempate determinístico por id desc.

    Mesma created_at nos dois registros; vence o id maior, nos dois sentidos,
    provando que a ordenação é total e determinística.
    """
    from uuid import UUID

    tied_at = datetime(2026, 1, 2, 3, 4, 5)
    a_low = UUID("11111111-1111-1111-1111-111111111111")
    a_high = UUID("11111111-1111-1111-1111-999999999999")
    b_low = UUID("22222222-2222-2222-2222-111111111111")
    b_high = UUID("22222222-2222-2222-2222-999999999999")

    order_a = make_order_without_payment(db, make_user, status="paid")
    db.session.add(
        Payment(
            id=a_low,
            order_id=order_a.id,
            provider_payment_id=f"mp_a_rej_{uuid4().hex[:8]}",
            status="rejected",
            amount=order_a.total,
            currency="BRL",
            created_at=tied_at,
        )
    )
    db.session.add(
        Payment(
            id=a_high,
            order_id=order_a.id,
            provider_payment_id=f"mp_a_appr_{uuid4().hex[:8]}",
            status="approved",
            amount=order_a.total,
            currency="BRL",
            created_at=tied_at,
        )
    )
    db.session.commit()
    # Vence o id maior (approved) → libera.
    assert _validate_payment_guard(order_a) is None

    order_b = make_order_without_payment(db, make_user, status="paid")
    db.session.add(
        Payment(
            id=b_low,
            order_id=order_b.id,
            provider_payment_id=f"mp_b_appr_{uuid4().hex[:8]}",
            status="approved",
            amount=order_b.total,
            currency="BRL",
            created_at=tied_at,
        )
    )
    db.session.add(
        Payment(
            id=b_high,
            order_id=order_b.id,
            provider_payment_id=f"mp_b_rej_{uuid4().hex[:8]}",
            status="rejected",
            amount=order_b.total,
            currency="BRL",
            created_at=tied_at,
        )
    )
    db.session.commit()

    refresher, submitter = SpyRefresher(), SpySubmitter()
    before = snapshot_counts()
    # Vence o id maior (rejected) → bloqueia.
    assert (
        _validate_payment_guard(order_b)
        == ERROR_CODES["PAYMENT_GUARD_NO_APPROVED_PAYMENT"]
    )
    assert ensure_aliexpress_fulfillment(order_b, refresher=refresher) == []
    assert_no_effects_for(order_b.id, before, refresher, submitter)
