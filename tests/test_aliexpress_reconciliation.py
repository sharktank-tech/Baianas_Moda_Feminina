"""Testes de reconciliação de submissão AliExpress (Fase 8.0.5) — HARD-OFF.

Garante que:
- máquina ready -> submitting -> submitted segue intacta no sucesso;
- timeout/ambíguo vai para ``pending_reconciliation`` e NÃO volta a ``ready``;
- estado ambíguo NÃO permite nova chamada de criação (nem após outro submit);
- reconciliação find -> submitted (persiste ali_order_id, NÃO chama submitter);
- reconciliação not_found conclusiva -> retry seguro na MESMA AliExpressOrder,
  com snapshot preservado e nova attempt_key;
- reconciliação unknown/erro -> permanece bloqueado para criação (sem retry cego);
- submitted continua idempotente;
- credencial expirada continua usando o fluxo OAuth 8.0.4;
- item_integrity_error tem precedência sobre credencial/submission/reconciliation;
- alteração/remoção posterior de catálogo continua irrelevante;
- providers padrão efetuam 0 chamadas HTTP reais (HARD-OFF + bloqueio de rede).
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
    AliExpressOrderReconciliationRequest,
    AliExpressOrderReconciliationResult,
    AliExpressReconciliationUnavailable,
    HardOffOrderReconciler,
    get_default_order_reconciler,
    reconcile_aliexpress_order,
    RECONCILE_FOUND,
    RECONCILE_NOT_FOUND,
    RECONCILE_UNKNOWN,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionFailed,
    AliExpressSubmissionRequest,
    AliExpressSubmissionResult,
    AliExpressSubmissionUnknown,
    HardOffOrderSubmitter,
    get_default_order_submitter,
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
from aliexpress_snapshot_utils import materialize_snapshot


@pytest.fixture(autouse=True)
def _valid_credential_env(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    """Bloqueia qualquer conexão de rede durante os testes da Fase 8.0.5."""

    def _reject(*args, **kwargs):
        raise AssertionError(
            "tentativa de conexão de rede durante teste da Fase 8.0.5"
        )

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class FakeSubmitter(AliExpressOrderSubmitter):
    """Provider de submission scriptado: success | failed | unknown | raise."""

    def __init__(self, *, result=None, exc=None, external_order_id=None):
        self.calls = []
        self.result = result
        self.exc = exc
        self.external_order_id = external_order_id or "AE-805"

    def submit(self, request: AliExpressSubmissionRequest):
        self.calls.append(request)
        if self.exc is AliExpressSubmissionFailed:
            raise AliExpressSubmissionFailed("falha confirmada do provider")
        if self.exc is AliExpressSubmissionUnknown:
            raise AliExpressSubmissionUnknown("timeout sem confirmação")
        if self.exc is not None:
            raise self.exc("erro inesperado com segredo=SUPER_805")
        return AliExpressSubmissionResult(external_order_ids=(self.external_order_id,))


class FakeReconciler(AliExpressOrderReconciler):
    """Provider de reconciliação scriptado (nunca faz rede)."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def reconcile(self, request: AliExpressOrderReconciliationRequest):
        self.calls.append(request)
        if self.exc is AliExpressReconciliationUnavailable:
            raise AliExpressReconciliationUnavailable("consulta indisponível")
        if self.exc is not None:
            raise self.exc("erro de reconciliação")
        return self.result


@pytest.fixture()
def ae_supplier(db):
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp Rec 8.0.5",
        channel_type="aliexpress",
        seller_id="SELLER_REC_805",
        default_currency="USD",
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


@pytest.fixture()
def ae_product(db):
    product = Product(
        id=uuid.uuid4(),
        name="Prod Rec 8.0.5",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="EXT_REC_805",
        is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture()
def sp_variant_factory(db):
    """Cria SupplierProduct + ProductVariant (histórico do snapshot)."""

    def _create(
        supplier,
        product,
        *,
        sku="SKU_REC",
        sp_id="100000001",
        cost=Decimal("10.00"),
        currency="USD",
    ):
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
            attributes=[
                {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
                {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
            ],
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
        access_token="tok805",
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    db.session.commit()


def _make_paid_order_with_item(
    db, make_user, product, variant, *, total=Decimal("29.90")
):
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="paid",
        subtotal=Decimal("29.90"),
        total=total,
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
    """Pedido pago + AliExpressOrder em ``ready`` (snapshot canônico criado)."""
    _persist_credential(db, supplier)
    order = _make_paid_order_with_item(db, make_user, product, variant)
    results = ensure_aliexpress_fulfillment(order)
    assert results[0].state == "ready"
    db.session.commit()
    ali = AliExpressOrder.query.filter_by(order_id=order.id).first()
    materialize_snapshot(ali, session=db.session)
    return ali, order


def _snapshot(db, ali):
    return AliExpressOrderItem.query.filter_by(
        aliexpress_order_id=ali.id
    ).all()


class TestSubmissionMachine:
    def test_sucesso_normal_ready_submitting_submitted(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        assert ali.state == "ready"

        fake = FakeSubmitter(external_order_id="AE-OK-1")
        result = submit_aliexpress_order(ali, submitter=fake)
        assert result.state == "submitted"
        assert result.aliexpress_order.ali_order_id == "AE-OK-1"
        assert len(fake.calls) == 1
        assert fake.calls[0].attempt_key is not None
        db.session.expire_all()

    def test_erro_ambiguo_nao_volta_para_ready(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)

        fake = FakeSubmitter(exc=AliExpressSubmissionUnknown)
        result = submit_aliexpress_order(ali, submitter=fake)
        assert result.state == "pending_reconciliation"
        db.session.expire_all()
        ali2 = db.session.get(AliExpressOrder, ali.id)
        assert ali2.id == ali.id
        assert ali2.state == "pending_reconciliation"
        assert ali2.last_error == ERROR_CODES["SUBMISSION_UNKNOWN"]
        assert ali2.attempt_count == 1
        assert ali2.attempt_key is not None
        assert ali2.last_attempt_at is not None
        assert ali2.ali_order_id is None
        items = _snapshot(db, ali2)
        assert len(items) == 1

    def test_estado_ambiguo_nao_permite_nova_criacao(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        first = FakeSubmitter(exc=AliExpressSubmissionUnknown)
        submit_aliexpress_order(ali, submitter=first)
        db.session.commit()

        second = FakeSubmitter(external_order_id="AE-FORBIDDEN")
        result = submit_aliexpress_order(ali, submitter=second)
        assert second.calls == []
        assert result.state == "pending_reconciliation"
        db.session.expire_all()
        ali2 = db.session.get(AliExpressOrder, ali.id)
        assert ali2.ali_order_id is None
        assert ali2.state == "pending_reconciliation"

    def test_erro_interno_generico_tratado_como_ambiguo(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory, caplog
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        caplog.set_level(logging.ERROR, logger="loja.integrations.aliexpress.submission")

        fake = FakeSubmitter(exc=RuntimeError)
        result = submit_aliexpress_order(ali, submitter=fake)
        assert result.state == "pending_reconciliation"
        assert result.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_UNKNOWN"]
        assert "SUPER_805" not in caplog.text

    def test_falha_confirmada_mantem_submitting_e_permite_retry(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)

        failed = FakeSubmitter(exc=AliExpressSubmissionFailed)
        r1 = submit_aliexpress_order(ali, submitter=failed)
        assert r1.state == "submitting"
        assert r1.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_FAILED"]
        db.session.commit()

        ok = FakeSubmitter(external_order_id="AE-RETRY")
        r2 = submit_aliexpress_order(ali, submitter=ok)
        assert r2.state == "submitted"
        assert r2.aliexpress_order.ali_order_id == "AE-RETRY"


class TestReconcileService:
    def test_reconcile_encontra_pedido_submitted(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        )
        db.session.commit()

        reconciler = FakeReconciler(
            AliExpressOrderReconciliationResult(
                outcome=RECONCILE_FOUND, external_order_id="REMOTE-1"
            )
        )
        result = reconcile_aliexpress_order(ali, reconciler=reconciler)
        assert result.state == "submitted"
        assert len(reconciler.calls) == 1
        assert result.aliexpress_order.ali_order_id == "REMOTE-1"
        db.session.expire_all()
        ali2 = db.session.get(AliExpressOrder, ali.id)
        assert ali2.submitted_at is not None
        assert all(i.state == "submitted" for i in _snapshot(db, ali2))

    def test_reconcile_encontrada_nao_chama_submitter_novamente(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        )
        db.session.commit()
        reconcile_aliexpress_order(
            ali,
            reconciler=FakeReconciler(
                AliExpressOrderReconciliationResult(
                    outcome=RECONCILE_FOUND, external_order_id="REMOTE-2"
                )
            ),
        )
        db.session.commit()

        submitter = FakeSubmitter(external_order_id="AE-NOPE")
        result = submit_aliexpress_order(ali, submitter=submitter)
        assert submitter.calls == []
        assert result.state == "submitted"
        assert result.aliexpress_order.ali_order_id == "REMOTE-2"

    def test_reconcile_conclusivo_not_found_permite_retry(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        attempt1_key = submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        ).aliexpress_order.attempt_key
        assert attempt1_key is not None
        db.session.commit()

        result = reconcile_aliexpress_order(
            ali,
            reconciler=FakeReconciler(
                AliExpressOrderReconciliationResult(outcome=RECONCILE_NOT_FOUND)
            ),
        )
        assert result.state == "ready"
        db.session.expire_all()
        ali2 = db.session.get(AliExpressOrder, ali.id)
        assert ali2.state == "ready"
        assert ali2.ali_order_id is None
        assert ali2.id == ali.id

        ok = FakeSubmitter(external_order_id="AE-RETRY-OK")
        r2 = submit_aliexpress_order(ali2, submitter=ok)
        assert r2.state == "submitted"
        assert r2.aliexpress_order.ali_order_id == "AE-RETRY-OK"
        assert r2.aliexpress_order.attempt_key != attempt1_key
        assert r2.aliexpress_order.id == ali.id
        assert len(_snapshot(db, r2.aliexpress_order)) == 1

    def test_retry_seguro_preserva_snapshot(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(
            ae_supplier, ae_product, sku="SKU_SNAP", sp_id="100000101"
        )
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        )
        db.session.commit()
        before = _snapshot(db, ali)

        # Catálogo muda DURANTE o ciclo ambiguo -> not_found -> retry.
        variant.supplier_sku_id = "SKU_MUTADO"
        sp.supplier_product_id = "SP_MUTADO"
        variant.price = Decimal("999.00")
        db.session.commit()

        reconcile_aliexpress_order(
            ali,
            reconciler=FakeReconciler(
                AliExpressOrderReconciliationResult(outcome=RECONCILE_NOT_FOUND)
            ),
        )
        r = submit_aliexpress_order(ali, submitter=FakeSubmitter(external_order_id="AE-SNAP"))
        assert r.state == "submitted"
        after = _snapshot(db, r.aliexpress_order)
        assert [i.id for i in before] == [i.id for i in after]
        assert after[0].supplier_sku_external_id == "SKU_SNAP"
        assert after[0].supplier_product_external_id == "100000101"
        assert after[0].supplier_unit_cost == Decimal("10.00")

    def test_reconcile_inconclusiva_unknown_continua_bloqueando_submit(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        )
        db.session.commit()

        r1 = reconcile_aliexpress_order(
            ali,
            reconciler=FakeReconciler(
                AliExpressOrderReconciliationResult(outcome=RECONCILE_UNKNOWN)
            ),
        )
        assert r1.state == "pending_reconciliation"
        assert r1.aliexpress_order.last_error == ERROR_CODES["RECONCILIATION_UNKNOWN"]

        submitter = FakeSubmitter(external_order_id="AE-BLOCKED")
        r2 = submit_aliexpress_order(ali, submitter=submitter)
        assert submitter.calls == []
        assert r2.state == "pending_reconciliation"
        assert r2.aliexpress_order.ali_order_id is None

    def test_reconcile_erro_mantem_bloqueio_e_sanitiza(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory, caplog
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        )
        db.session.commit()
        caplog.set_level(
            logging.WARNING, logger="loja.integrations.aliexpress.reconciliation"
        )

        r1 = reconcile_aliexpress_order(
            ali,
            reconciler=FakeReconciler(exc=AliExpressReconciliationUnavailable),
        )
        assert r1.state == "pending_reconciliation"
        assert r1.aliexpress_order.last_error == ERROR_CODES["RECONCILIATION_ERROR"]
        assert "consulta indisponível" not in caplog.text

        r2 = reconcile_aliexpress_order(
            ali, reconciler=FakeReconciler(exc=RuntimeError)
        )
        assert r2.state == "pending_reconciliation"
        assert r2.aliexpress_order.last_error == ERROR_CODES["RECONCILIATION_ERROR"]
        assert "SUPER_805" not in caplog.text
        assert r2.aliexpress_order.ali_order_id is None

    def test_submitted_continua_idempotente(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        first = FakeSubmitter(external_order_id="AE-IDEM")
        submit_aliexpress_order(ali, submitter=first)
        db.session.commit()

        second = FakeSubmitter(external_order_id="AE-OUTRO")
        result = submit_aliexpress_order(ali, submitter=second)
        assert second.calls == []
        assert result.state == "submitted"
        assert result.aliexpress_order.ali_order_id == "AE-IDEM"
        assert AliExpressOrder.query.filter_by(order_id=order.id).count() == 1


class TestPrecedenceAndRegressions:
    def test_credencial_expirada_continua_fluxo_oauth_804(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        from loja.integrations.aliexpress.oauth_refresh import (
            AliExpressOAuthRefresher,
            AliExpressOAuthRefreshRequest,
            AliExpressOAuthRefreshResult,
        )

        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)
        _persist_credential(db, ae_supplier, expires_in=0, refresh_token="RT_805")
        r_blocked = ensure_aliexpress_fulfillment(order)
        assert r_blocked[0].state == "blocked"
        assert "credential_expired" in r_blocked[0].aliexpress_order.last_error
        db.session.commit()

        class Refresher(AliExpressOAuthRefresher):
            def __init__(self):
                self.calls = []

            def refresh(self, request: AliExpressOAuthRefreshRequest):
                self.calls.append(request)
                return AliExpressOAuthRefreshResult(
                    access_token="FRESCO", expires_in=3600
                )

        refresher = Refresher()
        r_ready = ensure_aliexpress_fulfillment(order, refresher=refresher)
        assert r_ready[0].state == "ready"
        assert len(refresher.calls) == 1
        db.session.commit()
        ali = AliExpressOrder.query.filter_by(order_id=order.id).first()

        materialize_snapshot(ali, session=db.session)
        result = submit_aliexpress_order(
            ali, submitter=FakeSubmitter(external_order_id="AE-OAUTH-OK")
        )
        assert result.state == "submitted"

    def test_item_integrity_error_nunca_chega_ao_provider(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        product_b = Product(
            id=uuid.uuid4(),
            name="Prod B Rec",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_REC_B",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product_b)
        db.session.commit()
        sp, variant_b = sp_variant_factory(ae_supplier, product_b)
        _persist_credential(db, ae_supplier)

        order = Order(
            id=uuid.uuid4(),
            user_id=make_user().id,
            checkout_type="product",
            status="paid",
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="U",
            customer_email="u@rec.test",
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
                product_id=ae_product.id,
                variant_id=variant_b.id,
                product_name=ae_product.name,
                quantity=1,
                unit_price=Decimal("29.90"),
                line_total=Decimal("29.90"),
            )
        )
        db.session.commit()

        r1 = ensure_aliexpress_fulfillment(order)
        assert r1[0].state == "blocked"
        assert ERROR_CODES["ITEM_INTEGRITY_ERROR"] in r1[0].aliexpress_order.last_error
        db.session.commit()
        ali = r1[0].aliexpress_order

        submitter = FakeSubmitter(external_order_id="AE-NOPE")
        rs = submit_aliexpress_order(ali, submitter=submitter)
        assert submitter.calls == []
        assert rs.state == "blocked"
        reconciler = FakeReconciler(
            AliExpressOrderReconciliationResult(outcome=RECONCILE_FOUND)
        )
        rr = reconcile_aliexpress_order(ali, reconciler=reconciler)
        assert reconciler.calls == []
        assert rr.state == "blocked"

    def test_reconcile_com_item_integrity_error_precede_reconciler(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        submit_aliexpress_order(
            ali, submitter=FakeSubmitter(exc=AliExpressSubmissionUnknown)
        )
        db.session.commit()
        # Simula snapshot com erro de integridade persistido.
        item = _snapshot(db, ali)[0]
        item.state = "blocked"
        item.last_error = ERROR_CODES["ITEM_INTEGRITY_ERROR"]
        db.session.commit()

        reconciler = FakeReconciler(
            AliExpressOrderReconciliationResult(outcome=RECONCILE_FOUND)
        )
        result = reconcile_aliexpress_order(ali, reconciler=reconciler)
        assert reconciler.calls == []
        assert result.state == "blocked"
        assert result.aliexpress_order.ali_order_id is None

    def test_catalogo_removido_posteriormente_continua_irrelevante(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(
            ae_supplier, ae_product, sku="SKU_CAT", sp_id="100000103"
        )
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)
        before = _snapshot(db, ali)

        # Remove produto do catálogo por completo.
        pid, vid, spid = ae_product.id, variant.id, sp.id
        db.session.delete(sp)
        db.session.flush()
        db.session.delete(variant)
        db.session.flush()
        db.session.delete(ae_product)
        db.session.commit()

        r1 = ensure_aliexpress_fulfillment(order)
        assert r1[0].state == "ready"
        r2 = submit_aliexpress_order(
            ali, submitter=FakeSubmitter(external_order_id="AE-CAT")
        )
        assert r2.state == "submitted"
        assert r2.aliexpress_order.supplier_id == ae_supplier.id
        ali_item = _snapshot(db, r2.aliexpress_order)[0]
        assert ali_item.supplier_sku_external_id == "SKU_CAT"
        assert ali_item.supplier_unit_cost == Decimal("10.00")
        assert ali_item.state == "submitted"

    def test_provider_padrao_efetua_0_chamadas_http_reais(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali, order = _materialize_ready(db, make_user, ae_supplier, ae_product, variant)

        assert isinstance(get_default_order_submitter(), HardOffOrderSubmitter)
        assert isinstance(get_default_order_reconciler(), HardOffOrderReconciler)

        result = submit_aliexpress_order(ali)
        assert result.state == "submitting"
        assert result.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_HARD_OFF"]
        db.session.commit()
        # Reconciliação hard-off não altera a máquina (0 chamadas HTTP reais).
        rnoop = reconcile_aliexpress_order(ali)
        assert rnoop.state == "submitting"
        db.session.commit()

        # Mesmo com pedido ambíguo, o reconciler padrão (hard-off) mantém o
        # bloqueio para criação e NUNCA trata como "não criado".
        submit_aliexpress_order(ali, submitter=FakeSubmitter(exc=RuntimeError))
        db.session.commit()
        r = reconcile_aliexpress_order(ali)
        assert r.state == "pending_reconciliation"
        assert r.aliexpress_order.last_error == ERROR_CODES["RECONCILIATION_ERROR"]
        assert r.aliexpress_order.ali_order_id is None