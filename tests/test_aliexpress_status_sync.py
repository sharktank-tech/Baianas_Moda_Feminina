"""Testes de sincronização de status pós-submissão AliExpress (Fase 8.0.6) — HARD-OFF.

Garante:
- default syncer é HardOffOrderStatusSyncer e efetua ZERO HTTP;
- ordens sem ali_order_id / estados inelegíveis NÃO consultam provider;
- pending_reconciliation nunca consulta provider (não resolve reconciliacao);
- máquina monótona submitted -> confirmed -> shipped -> delivered;
- mesmos status idempotentes; regressões ignoradas sem reescrita de timestamps;
- cancelled observado a partir de submitted/confirmed/shipped;
- delivered NÃO vira cancelled silenciosamente (transição inválida observada);
- unknown/erro mantém estado + last_error sanitizado (nunca segredo);
- ali_status contém apenas valor canônico/sanitizado;
- snapshot dos AliExpressOrderItem permanece intocado;
- RECONCILE_FOUND NÃO grava mais confirmed_at (correção 8.0.5);
- status sync confirmed após reconciliation found grava confirmed_at.
"""
import logging
import socket
import uuid
from decimal import Decimal

import pytest

from loja import db
from loja.integrations.aliexpress.fulfillment import (
    ERROR_CODES,
    ensure_aliexpress_fulfillment,
)
from loja.integrations.aliexpress.reconciliation import (
    AliExpressOrderReconciler,
    AliExpressOrderReconciliationResult,
    reconcile_aliexpress_order,
    RECONCILE_FOUND,
)
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusSyncer,
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusResult,
    AliExpressOrderStatusSyncError,
    AliExpressOrderStatusSyncUnavailable,
    HardOffOrderStatusSyncer,
    get_default_order_status_syncer,
    sync_aliexpress_order_status,
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_SHIPPED,
    STATUS_SUBMITTED,
    STATUS_CANCELLED,
    STATUS_UNKNOWN,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
    AliExpressSubmissionUnknown,
    submit_aliexpress_order,
)
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
)


@pytest.fixture(autouse=True)
def _valid_credential_env(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    """Bloqueia qualquer conexão de rede durante os testes da Fase 8.0.6."""

    def _reject(*args, **kwargs):
        raise AssertionError(
            "tentativa de conexão de rede durante teste da Fase 8.0.6"
        )

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class ScriptedSyncer(AliExpressOrderStatusSyncer):
    """Provider de status fake: devolve outcomes na ordem ou exceção."""

    def __init__(self, result=None, results=None, exc=None, external=None):
        self.calls = []
        self.result = result
        self.results = list(results or [])
        self.exc = exc
        self.external = external

    def _next_result(self):
        if self.results:
            self.result = self.results.pop(0)
        return self.result

    def sync(self, request: AliExpressOrderStatusRequest):
        self.calls.append(request)
        if self.exc is AliExpressOrderStatusSyncUnavailable:
            raise AliExpressOrderStatusSyncUnavailable("status indisponível")
        if self.exc is AliExpressOrderStatusSyncError:
            raise AliExpressOrderStatusSyncError("falha de status")
        if self.exc is not None:
            raise self.exc("erro inesperado com segredo=SUPER_806")
        outcome = self._next_result()
        return AliExpressOrderStatusResult(
            outcome=outcome, external_status=self.external
        )


class OkSubmitter(AliExpressOrderSubmitter):
    def __init__(self, external_order_id="AE-806"):
        self.calls = []
        self.external_order_id = external_order_id

    def submit(self, request):
        self.calls.append(request)
        return AliExpressSubmissionResult(external_order_id=self.external_order_id)


class UnknownSubmitter(AliExpressOrderSubmitter):
    def submit(self, request):
        raise AliExpressSubmissionUnknown("timeout sem confirmação")


class FoundReconciler(AliExpressOrderReconciler):
    def __init__(self, external_order_id="REMOTE-806"):
        self.external_order_id = external_order_id

    def reconcile(self, request):
        return AliExpressOrderReconciliationResult(
            outcome=RECONCILE_FOUND, external_order_id=self.external_order_id
        )


@pytest.fixture()
def ae_supplier(db):
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp Sync 8.0.6",
        channel_type="aliexpress",
        seller_id="SELLER_SYNC_806",
        default_currency="USD",
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


@pytest.fixture()
def ae_product(db):
    product = Product(
        id=uuid.uuid4(),
        name="Prod Sync 8.0.6",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="EXT_SYNC_806",
        is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture()
def sp_variant_factory(db):
    def _create(supplier, product, *, sku=None, sp_id=None, cost=Decimal("10.00"),
                currency="USD"):
        sku = sku or f"SKU_SYNC_{uuid.uuid4().hex[:8]}"
        sp_id = sp_id or f"SP_SYNC_{uuid.uuid4().hex[:8]}"
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id=sp_id,
            cost=cost,
            currency=currency,
            is_active=True,
        )
        db.session.add(sp)
        db.session.flush()
        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=sp.id,
            supplier_sku_id=sku,
            sku=None,
            color="Red",
            size="M",
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.commit()
        return sp, variant

    return _create


def _persist_credential(db, supplier, *, expires_in=3600, refresh_token=None):
    from loja.integrations.aliexpress.credential_store import persist_credential

    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok806",
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    db.session.commit()


def _make_paid_order_with_item(db, make_user, product, variant):
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="paid",
        subtotal=Decimal("29.90"),
        total=Decimal("29.90"),
        stock_reserved=False,
        customer_name="U",
        customer_email=user.email,
        customer_phone="11999999999",
        zip_code="01001000",
        street="Rua",
        number="1",
        complement="",
        neighborhood="C",
        city="SP",
        state="SP",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=product.id,
            variant_id=variant.id,
            product_name=product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
    )
    db.session.commit()
    return order


def _materialize_ready(db, make_user, supplier, product, variant):
    _persist_credential(db, supplier)
    order = _make_paid_order_with_item(db, make_user, product, variant)
    results = ensure_aliexpress_fulfillment(order)
    assert results[0].state == "ready"
    db.session.commit()
    ali = AliExpressOrder.query.filter_by(order_id=order.id).first()
    return ali, order


def _submit_ok(db, ali_order, external=None):
    external = external or f"AE-SUCC-{uuid.uuid4().hex[:10]}"
    result = submit_aliexpress_order(ali_order, submitter=OkSubmitter(external))
    db.session.commit()
    assert result.state == "submitted"
    db.session.expire_all()
    return db.session.get(AliExpressOrder, ali_order.id)


def _to_pending(db, ali_order):
    result = submit_aliexpress_order(ali_order, submitter=UnknownSubmitter())
    db.session.commit()
    assert result.state == "pending_reconciliation"
    db.session.expire_all()
    return db.session.get(AliExpressOrder, ali_order.id)


def _reconcile_found(db, ali_order, external=None):
    external = external or f"REMOTE-{uuid.uuid4().hex[:10]}"
    result = reconcile_aliexpress_order(
        ali_order, reconciler=FoundReconciler(external)
    )
    db.session.commit()
    assert result.state == "submitted"
    db.session.expire_all()
    return db.session.get(AliExpressOrder, ali_order.id)


def _sync(db, ali_order, **kw):
    use_default = kw.pop("use_default", False)
    syncer = None if use_default else ScriptedSyncer(**kw)
    result = sync_aliexpress_order_status(ali_order, syncer=syncer)
    db.session.commit()
    db.session.expire_all()
    fresh = db.session.get(AliExpressOrder, ali_order.id)
    return result, fresh


def _snapshot(db, ali):
    return AliExpressOrderItem.query.filter_by(aliexpress_order_id=ali.id).all()


class TestBoundaryAndEligibility:
    def test_default_syncer_e_hard_off(self):
        assert isinstance(get_default_order_status_syncer(), HardOffOrderStatusSyncer)

    def test_hard_off_faz_zero_http(self, db, make_user, ae_supplier,
                                    ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, use_default=True)
        assert result.state == "submitted"
        assert fresh.last_error == ERROR_CODES["STATUS_SYNC_HARD_OFF"]

    def test_sem_ali_order_id_nao_consulta_provider(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        syncer = ScriptedSyncer(result=STATUS_CONFIRMED)
        result = sync_aliexpress_order_status(ali, syncer=syncer)
        assert syncer.calls == []
        assert result.state == "ready"

    def test_pending_reconciliation_nunca_consulta_provider(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _to_pending(db, ali)
        syncer = ScriptedSyncer(result=STATUS_CONFIRMED)
        result = sync_aliexpress_order_status(ali, syncer=syncer)
        assert syncer.calls == []
        assert result.state == "pending_reconciliation"
        assert ali.ali_order_id is None

    def test_ready_nunca_consulta_provider(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        syncer = ScriptedSyncer(result=STATUS_SHIPPED)
        result = sync_aliexpress_order_status(ali, syncer=syncer)
        assert syncer.calls == []
        assert result.state == "ready"


class TestMonotonicMachine:
    def test_submitted_para_confirmed(self, db, make_user, ae_supplier,
                                      ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        assert result.state == "confirmed"
        assert fresh.state == "confirmed"
        assert fresh.ali_status == STATUS_CONFIRMED
        assert fresh.confirmed_at is not None
        assert fresh.last_error is None

    def test_submitted_para_shipped(self, db, make_user, ae_supplier,
                                    ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_SHIPPED)
        assert result.state == "shipped"
        assert fresh.confirmed_at is not None
        assert fresh.shipped_at is not None

    def test_submitted_para_delivered(self, db, make_user, ae_supplier,
                                      ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_DELIVERED)
        assert result.state == "delivered"
        assert fresh.confirmed_at is not None
        assert fresh.shipped_at is not None
        assert fresh.delivered_at is not None

    def test_confirmed_para_shipped(self, db, make_user, ae_supplier,
                                    ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        assert fresh.state == "confirmed"
        result, fresh2 = _sync(db, fresh, result=STATUS_SHIPPED)
        assert fresh2.state == "shipped"
        assert fresh2.shipped_at is not None

    def test_confirmed_para_delivered(self, db, make_user, ae_supplier,
                                      ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        result, fresh2 = _sync(db, fresh, result=STATUS_DELIVERED)
        assert fresh2.state == "delivered"
        assert fresh2.delivered_at is not None

    def test_shipped_para_delivered(self, db, make_user, ae_supplier,
                                    ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_SHIPPED)
        assert fresh.state == "shipped"
        result, fresh2 = _sync(db, fresh, result=STATUS_DELIVERED)
        assert fresh2.state == "delivered"

    def test_mesmo_status_idempotente(self, db, make_user, ae_supplier,
                                      ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_SUBMITTED)
        assert result.state == "submitted"
        assert fresh.state == "submitted"


class TestNoRegression:
    def test_shipped_nao_regride_para_confirmed(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_SHIPPED)
        shipped_at = fresh.shipped_at
        result, fresh2 = _sync(db, fresh, result=STATUS_CONFIRMED)
        assert result.state == "shipped"
        assert fresh2.state == "shipped"
        assert fresh2.shipped_at == shipped_at

    def test_delivered_nao_regride_para_shipped(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_DELIVERED)
        assert result.state == "delivered"
        delivered_at = fresh.delivered_at
        result2, fresh2 = _sync(db, fresh, result=STATUS_SHIPPED)
        assert result2.state == "delivered"
        assert fresh2.delivered_at == delivered_at

    def test_confirmed_nao_regride_para_submitted(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        assert result.state == "confirmed"
        result2, fresh2 = _sync(db, fresh, result=STATUS_SUBMITTED)
        assert result2.state == "confirmed"
        assert fresh2.state == "confirmed"


class TestMilestones:
    def test_confirmed_at_preenchido_uma_vez(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        c1 = fresh.confirmed_at
        result, fresh2 = _sync(db, fresh, result=STATUS_CONFIRMED)
        assert fresh2.confirmed_at == c1

    def test_shipped_at_preenchido_uma_vez(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_SHIPPED)
        s1 = fresh.shipped_at
        result, fresh2 = _sync(db, fresh, result=STATUS_SHIPPED)
        assert fresh2.shipped_at == s1

    def test_delivered_at_preenchido_uma_vez(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_DELIVERED)
        d1 = fresh.delivered_at
        result, fresh2 = _sync(db, fresh, result=STATUS_DELIVERED)
        assert fresh2.delivered_at == d1

    def test_sync_repetido_nao_altera_timestamps(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_DELIVERED)
        c1, s1, d1 = fresh.confirmed_at, fresh.shipped_at, fresh.delivered_at
        for _ in range(3):
            _, fresh = _sync(db, fresh, result=STATUS_DELIVERED)
        assert (fresh.confirmed_at, fresh.shipped_at, fresh.delivered_at) == (c1, s1, d1)


class TestCancelled:
    def test_cancelled_remoto_a_partir_de_submitted(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_CANCELLED)
        assert result.state == "cancelled"
        assert fresh.cancelled_at is not None
        assert fresh.ali_status == STATUS_CANCELLED
        assert fresh.last_error is None

    def test_cancelled_remoto_a_partir_de_confirmed(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        result, fresh2 = _sync(db, fresh, result=STATUS_CANCELLED)
        assert result.state == "cancelled"
        assert fresh2.cancelled_at is not None

    def test_cancelled_remoto_a_partir_de_shipped(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        _, fresh = _sync(db, ali, result=STATUS_SHIPPED)
        result, fresh2 = _sync(db, fresh, result=STATUS_CANCELLED)
        assert result.state == "cancelled"

    def test_delivered_nao_vira_cancelled_silenciosamente(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_DELIVERED)
        assert result.state == "delivered"
        delivered_at = fresh.delivered_at
        result2, fresh2 = _sync(db, fresh, result=STATUS_CANCELLED)
        assert result2.state == "delivered"
        assert fresh2.state == "delivered"
        assert fresh2.cancelled_at is None
        assert fresh2.delivered_at == delivered_at
        assert fresh2.last_error == ERROR_CODES["STATUS_SYNC_INVALID_TRANSITION"]


class TestErrorsAndSanitization:
    def test_unknown_mantem_estado(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_UNKNOWN)
        assert result.state == "submitted"
        assert fresh.state == "submitted"
        assert fresh.last_error == ERROR_CODES["STATUS_SYNC_UNKNOWN"]

    def test_erro_mantem_estado(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, exc=AliExpressOrderStatusSyncError)
        assert result.state == "submitted"
        assert fresh.last_error == ERROR_CODES["STATUS_SYNC_FAILED"]

    def test_last_error_sanitizado_para_exception_generica(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory, caplog
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        caplog.set_level(logging.ERROR, logger="loja.integrations.aliexpress.status_sync")
        result, fresh = _sync(db, ali, exc=RuntimeError)
        assert result.state == "submitted"
        assert fresh.last_error == ERROR_CODES["STATUS_SYNC_ERROR"]
        assert "SUPER_806" not in caplog.text

    def test_exception_com_secret_nao_aparece_em_log_nem_error(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory, caplog
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        caplog.set_level(logging.ERROR, logger="loja.integrations.aliexpress.status_sync")

        class EvilError(Exception):
            def __str__(self):
                return "secret=TOKEN_VILAO_806 header=Bearer XIX"

        result, fresh = _sync(db, ali, exc=EvilError)
        assert result.state == "submitted"
        assert "TOKEN_VILAO_806" not in (fresh.last_error or "")
        assert "TOKEN_VILAO_806" not in caplog.text
        assert "Bearer" not in (fresh.last_error or "")

    def test_ali_status_contem_apenas_valor_sanitizado_canonico(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _submit_ok(db, ali)
        # external_status deve ser ignorado para ali_status (só outcome canônico).
        result, fresh = _sync(
            db, ali, result=STATUS_CONFIRMED, external="RAW-1FZJQ@secret"
        )
        assert result.state == "confirmed"
        assert fresh.ali_status == STATUS_CONFIRMED
        assert "RAW-1FZJQ" not in fresh.ali_status


class TestSnapshotAndReconciliationCorrection:
    def test_snapshot_dos_itens_permance_intocado(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(
            ae_supplier, ae_product, sku="SKU_SNAP806", sp_id="SP_SNAP806"
        )
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        before = _snapshot(db, ali)
        ali = _submit_ok(db, ali)
        result, fresh = _sync(db, ali, result=STATUS_DELIVERED)
        assert result.state == "delivered"
        after = _snapshot(db, fresh)
        assert [i.id for i in before] == [i.id for i in after]
        assert after[0].supplier_sku_external_id == "SKU_SNAP806"
        assert after[0].supplier_product_external_id == "SP_SNAP806"
        assert after[0].supplier_unit_cost == Decimal("10.00")

    def test_reconcile_found_nao_grava_mais_confirmed_at(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _to_pending(db, ali)
        result = reconcile_aliexpress_order(ali, reconciler=FoundReconciler())
        db.session.commit()
        db.session.expire_all()
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert result.state == "submitted"
        assert fresh.state == "submitted"
        assert fresh.confirmed_at is None

    def test_status_sync_confirmed_apos_reconciliation_grava_confirmed_at(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        ali = _to_pending(db, ali)
        ali = _reconcile_found(db, ali)
        assert ali.confirmed_at is None
        result, fresh = _sync(db, ali, result=STATUS_CONFIRMED)
        assert result.state == "confirmed"
        assert fresh.confirmed_at is not None
        assert fresh.state == "confirmed"