"""Testes do pipeline de submissão AliExpress (Fase 8.0.3) — HARD-OFF.

Garante que:
- o núcleo só fala com o provider através do boundary ``AliExpressOrderSubmitter``;
- o payload de submissão vem SOMENTE do snapshot persistido (``AliExpressOrder`` +
  ``AliExpressOrderItem``) e nunca do catálogo;
- a máquina de estados é ``ready -> submitting -> submitted``, com ``submitting``
  persistido antes da tentativa e nenhuma transição direta ``ready -> submitted``;
- idempotência: ``submitted`` retorna o resultado persistido sem chamar o provider
  nem gerar nova identidade externa;
- ordens inválidas (blocked, item_integrity_error, credencial ausente/expirada)
  nunca chegam ao provider;
- falha preserva ordem/itens/id/snapshot e permite retry síncrono;
- nenhum teste toca rede (autouse bloqueadora).
"""
import socket
import uuid
from decimal import Decimal

import pytest

from loja import db
from loja.integrations.aliexpress.fulfillment import (
    ERROR_CODES,
    ensure_aliexpress_fulfillment,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionFailed,
    AliExpressSubmissionResult,
    AliExpressSubmissionUnknown,
    HardOffOrderSubmitter,
    submit_aliexpress_order,
)
from aliexpress_snapshot_utils import materialize_snapshot
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
    """Credencial enc criptografia funcional p/ persistir tokens nos testes."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    """Bloqueia qualquer conexão de rede durante os testes da Fase 8.0.3."""

    def _reject(*args, **kwargs):
        raise AssertionError(
            "tentativa de conexão de rede durante teste da Fase 8.0.3"
        )

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class FakeSubmitter(AliExpressOrderSubmitter):
    """Provider de teste instrumentado (nunca faz rede)."""

    def __init__(self, *, fail_times=0, external_order_id=None):
        self.calls = []
        self.fail_times = fail_times
        self.external_order_id = external_order_id or f"AE-{uuid.uuid4().hex[:12]}"
        self.last_request: AliExpressSubmissionRequest | None = None

    def submit(self, request):
        self.calls.append(request)
        self.last_request = request
        if self.fail_times > 0:
            self.fail_times -= 1
            raise AliExpressSubmissionFailed("falha transiente simulada")
        return AliExpressSubmissionResult(
            external_order_id=self.external_order_id
        )


@pytest.fixture()
def ae_supplier(db):
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp 8.0.3",
        channel_type="aliexpress",
        seller_id="SELLER_803",
        default_currency="USD",
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


@pytest.fixture()
def ae_product(db):
    product = Product(
        id=uuid.uuid4(),
        name="Prod 8.0.3",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="EXT_803",
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
        sku="SKU_HIST",
        sp_id="SP_HIST",
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


def _persist_valid_credential(db, supplier):
    from loja.integrations.aliexpress.credential_store import persist_credential

    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok",
        expires_in=3600,
    )
    db.session.commit()


def _materialize_ready(
    db,
    make_user,
    supplier,
    product,
    variant,
    *,
    quantity=2,
    total=Decimal("59.80"),
):
    """Cria pedido pago com item AliExpress e devolve o AliExpressOrder ready."""
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
    item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        product_id=product.id,
        variant_id=variant.id,
        product_name=product.name,
        quantity=quantity,
        unit_price=Decimal("29.90"),
        line_total=Decimal("29.90") * quantity,
    )
    db.session.add(item)
    db.session.commit()

    results = ensure_aliexpress_fulfillment(order)
    assert len(results) == 1
    assert results[0].state == "ready"
    db.session.commit()
    ali_order = results[0].aliexpress_order
    materialize_snapshot(ali_order, session=db.session)
    return ali_order, order, item


def _materialize_ready_without_snap(
    db,
    make_user,
    supplier,
    product,
    variant,
    *,
    quantity=2,
    total=Decimal("59.80"),
):
    """Igual a _materialize_ready, MAS sem materializar o submission snapshot.

    Serve para provar o gate canônico: submission sem snapshot NÃO chama
    provider, NÃO gera attempt_key e mantém ``ready``.
    """
    _persist_valid_credential(db, supplier)
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
    item = OrderItem(
        id=uuid.uuid4(),
        order_id=order.id,
        product_id=product.id,
        variant_id=variant.id,
        product_name=product.name,
        quantity=quantity,
        unit_price=Decimal("29.90"),
        line_total=Decimal("29.90") * quantity,
    )
    db.session.add(item)
    db.session.commit()

    results = ensure_aliexpress_fulfillment(order)
    assert len(results) == 1
    assert results[0].state == "ready"
    db.session.commit()
    return results[0].aliexpress_order, order, item


class TestHardOffSubmitter:
    def test_hard_off_blocks_and_persists_submitting(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """Envio real é bloqueado de forma determinística; submitting persistido;
        nenhuma identidade externa é gravada."""
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )

        result = submit_aliexpress_order(ali_order)

        assert result.state == "submitting"
        assert result.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_HARD_OFF"]
        assert result.aliexpress_order.ali_order_id is None
        assert result.aliexpress_order.submitted_at is None
        assert result.aliexpress_order.attempt_count >= 1
        db.session.refresh(ali_order)
        assert ali_order.state == "submitting"


class TestFakeSubmissionFlow:
    def test_ready_submitting_submitted_with_fake_success(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """fake sucesso: ready -> submitting -> submitted com identidade externa."""
        from loja.integrations.aliexpress.credential_store import persist_credential

        persist_credential(
            supplier_id=ae_supplier.id,
            seller_id=ae_supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.commit()
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        assert ali_order.state == "ready"

        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)

        assert result.state == "submitted"
        assert len(fake.calls) == 1
        db.session.refresh(ali_order)
        assert ali_order.state == "submitted"
        assert ali_order.ali_order_id == fake.external_order_id
        assert ali_order.submitted_at is not None
        assert ali_order.last_error is None
        items = AliExpressOrderItem.query.filter_by(
            aliexpress_order_id=ali_order.id
        ).all()
        assert len(items) == 1
        assert items[0].state == "submitted"

    def test_submitted_is_idempotent_no_new_external_submission(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )

        fake = FakeSubmitter()
        r1 = submit_aliexpress_order(ali_order, submitter=fake)
        assert r1.state == "submitted"
        external = r1.aliexpress_order.ali_order_id

        fake2 = FakeSubmitter(external_order_id="AE-SECOND")
        r2 = submit_aliexpress_order(ali_order, submitter=fake2)

        assert r2.state == "submitted"
        assert r2.aliexpress_order.ali_order_id == external
        assert fake2.calls == []
        assert len(fake.calls) == 1
        # Uma única submissão externa: o id externo persistido é imutável.
        db.session.refresh(ali_order)
        assert ali_order.ali_order_id == external

    def test_two_sequential_attempts_single_external_submission(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )

        fake = FakeSubmitter()
        submit_aliexpress_order(ali_order, submitter=fake)
        submit_aliexpress_order(ali_order, submitter=fake)

        assert len(fake.calls) == 1
        db.session.refresh(ali_order)
        assert ali_order.state == "submitted"


class TestInvalidOrdersNeverSubmitted:
    def test_blocked_order_never_submitted(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """Sem credencial -> blocked (credential_missing); submissão não ocorre."""
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
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
        db.session.add(OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=ae_product.id,
            variant_id=variant.id,
            product_name=ae_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        ))
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order)
        assert results[0].state == "blocked"
        ali_order = results[0].aliexpress_order
        db.session.commit()

        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)

        assert fake.calls == []
        assert result.state == "blocked"
        assert result.aliexpress_order.ali_order_id is None

    def test_item_integrity_error_never_submitted(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """Cross-product (variant de outro produto) nunca é submetido, mesmo com
        credencial válida (integridade prevalece)."""
        product_b = Product(
            id=uuid.uuid4(),
            name="Prod B 8.0.3",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_803B",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product_b)
        db.session.commit()
        _persist_valid_credential(db, ae_supplier)
        sp, variant_b = sp_variant_factory(ae_supplier, product_b)

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
        # OrderItem referencia PRODUTO A mas variante do PRODUTO B.
        db.session.add(OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=ae_product.id,
            variant_id=variant_b.id,
            product_name=ae_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        ))
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order)
        ali_order = results[0].aliexpress_order
        assert ali_order.state == "blocked"
        assert ERROR_CODES["ITEM_INTEGRITY_ERROR"] in ali_order.last_error
        db.session.commit()

        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)

        assert fake.calls == []
        assert result.state == "blocked"
        assert ERROR_CODES["ITEM_INTEGRITY_ERROR"] in result.aliexpress_order.last_error
        assert result.aliexpress_order.ali_order_id is None

    def test_expired_credential_blocks_submission(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        from loja.integrations.aliexpress.credential_store import persist_credential

        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )

        persist_credential(
            supplier_id=ae_supplier.id,
            seller_id=ae_supplier.seller_id,
            access_token="tok",
            expires_in=0,
        )
        db.session.commit()

        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)

        assert fake.calls == []
        assert result.aliexpress_order.last_error == ERROR_CODES["CREDENTIAL_EXPIRED"]
        assert result.aliexpress_order.ali_order_id is None
        # Estado da dimensão fulfillment preservado (ready), apenas sem enviar.
        assert result.state == "ready"


class TestFailureAndRetry:
    def test_failure_does_not_recreate_order_and_preserves_snapshot(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, item = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        ali_id = ali_order.id
        snap_before = AliExpressOrderItem.query.filter_by(
            aliexpress_order_id=ali_id
        ).first()

        fake = FakeSubmitter(fail_times=1, external_order_id="AE-EXTERNAL-1")
        result = submit_aliexpress_order(ali_order, submitter=fake)

        assert result.state == "submitting"
        assert result.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_FAILED"]
        assert result.aliexpress_order.ali_order_id is None
        assert result.aliexpress_order.attempt_count >= 1

        # Mesma ordem, nada recriado.
        rows = AliExpressOrder.query.filter_by(id=ali_id).all()
        assert len(rows) == 1
        assert rows[0].id == ali_id
        dupes = AliExpressOrder.query.filter(
            AliExpressOrder.id != ali_id,
            AliExpressOrder.order_id == ali_order.order_id,
        ).all()
        assert dupes == []

        # Snapshot preservado integralmente.
        snap_after = AliExpressOrderItem.query.filter_by(
            aliexpress_order_id=ali_id
        ).first()
        assert snap_after.supplier_product_external_id == snap_before.supplier_product_external_id
        assert snap_after.supplier_sku_external_id == snap_before.supplier_sku_external_id
        assert snap_after.quantity == snap_before.quantity
        assert snap_after.supplier_unit_cost == snap_before.supplier_unit_cost
        assert snap_after.supplier_currency == snap_before.supplier_currency
        assert snap_after.variant_label == snap_before.variant_label

    def test_retry_after_failure_succeeds_and_keeps_same_order_id(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        ali_id = ali_order.id
        db.session.expire_all()

        fake = FakeSubmitter(fail_times=1, external_order_id="AE-EXTERNAL-1")
        r1 = submit_aliexpress_order(ali_order, submitter=fake)
        assert r1.state == "submitting"
        assert r1.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_FAILED"]

        r2 = submit_aliexpress_order(ali_order, submitter=fake)
        assert r2.state == "submitted"
        assert r2.aliexpress_order.id == ali_id
        assert r2.aliexpress_order.ali_order_id == fake.external_order_id
        assert len(fake.calls) == 2
        db.session.refresh(ali_order)
        assert ali_order.state == "submitted"
        assert ali_order.id == ali_id

    def test_retry_after_hard_off_with_fake_succeeds(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """Hard-off falha primeiro; retry com fake injetado conclui a submissão."""
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        ali_id = ali_order.id

        r1 = submit_aliexpress_order(ali_order)
        assert r1.state == "submitting"
        assert r1.aliexpress_order.last_error == ERROR_CODES["SUBMISSION_HARD_OFF"]

        r2 = submit_aliexpress_order(
            ali_order, submitter=FakeSubmitter(external_order_id="AE-EXTERNAL-HO")
        )
        assert r2.state == "submitted"
        assert r2.aliexpress_order.id == ali_id
        assert r2.aliexpress_order.ali_order_id == "AE-EXTERNAL-HO"


class TestSnapshotPayload:
    def test_payload_uses_snapshot_not_catalog_after_mutation_and_deletion(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """O payload de submissão usa o snapshot histórico mesmo com catálogo
        mutado e produto removido (FK SET NULL)."""
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(
            ae_supplier, ae_product,
            sku="SKU_HIST", sp_id="SP_HIST",
            cost=Decimal("10.00"), currency="USD",
        )
        ali_order, _, item = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant, quantity=2
        )

        # Mutar o catálogo após o snapshot.
        variant.supplier_sku_id = "SKU_NEW"
        variant.color = "Blue"
        sp.supplier_product_id = "SP_NEW"
        sp.cost = Decimal("999.00")
        sp.currency = "EUR"
        sp.is_active = False
        db.session.commit()

        # Remover Product (e, por FK, SupplierProduct/variante).
        db.session.delete(sp)
        db.session.flush()
        db.session.delete(variant)
        db.session.flush()
        db.session.delete(ae_product)
        db.session.commit()
        db.session.expire_all()

        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "submitted"

        request = fake.last_request
        assert request is not None
        assert len(request.items) == 1
        payload = request.items[0]
        # O payload canônico vem 100% do snapshot (product_id/sku_attr/count),
        # nunca do catálogo mutado/removido neste instante.
        assert payload.product_id == "SP_HIST"
        assert payload.sku_attr == "14:771;200000124:100013888"
        assert payload.product_count == 2
        assert payload.logistics_service_name == "CAINIAO"
        assert request.shipping_address["zip"] == "01001000"
        assert request.shipping_address["address"] == "Rua 1 C"
        assert request.shipping_address["city"] == "SP"
        assert request.shipping_address["province"] == "SP"
        assert request.shipping_address["contact_person"] == "U"
        assert request.shipping_address["country"] == "BR"


class TestSubmissionSnapshotPreflight:
    """Fase 8.0.10.2 — gate canônico: submission exige snapshot válido."""

    def test_submission_without_snapshot_blocks(self, db, make_user, ae_supplier,
                                                ae_product, sp_variant_factory):
        """Sem snapshot válido, submissão fica em ``ready``, não gera attempt_key
        nem chama provider, e persiste ``submission_snapshot_missing``."""
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready_without_snap(
            db, make_user, ae_supplier, ae_product, variant
        )
        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "ready"
        assert fake.calls == []
        fresh = db.session.get(AliExpressOrder, ali_order.id)
        assert fresh.attempt_key is None
        assert fresh.attempt_count == 0
        assert fresh.state == "ready"
        assert "submission_snapshot_missing" in fresh.last_error

    def test_valid_snapshot_allows_submission(self, db, make_user, ae_supplier,
                                              ae_product, sp_variant_factory):
        """Com snapshot válido, submissão é autorizada e o payload é canônico."""
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant, quantity=2
        )
        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "submitted"
        assert len(fake.calls) == 1

    def test_provider_failure_when_valid_snapshot_allows_retry(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        """Falha confirmada com snapshot válido mantém submitting e permite retry."""
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        fake = FakeSubmitter(fail_times=1)
        r1 = submit_aliexpress_order(ali_order, submitter=fake)
        assert r1.state == "submitting"
        assert ERROR_CODES["SUBMISSION_FAILED"] in r1.aliexpress_order.last_error
        fake2 = FakeSubmitter()
        r2 = submit_aliexpress_order(ali_order, submitter=fake2)
        assert r2.state == "submitted"
        assert len(fake2.calls) == 1

    def _inject_invalid_snapshot(self, db, ali_order, **mutations):
        """Muta o JSONB do snapshot para simular estrutura inválida."""
        from loja.integrations.aliexpress.submission_snapshot import (
            AliExpressSubmissionSnapshot,
        )

        snap = db.session.query(AliExpressSubmissionSnapshot).filter_by(
            aliexpress_order_id=ali_order.id
        ).first()
        assert snap is not None
        items = list(snap.items)
        entry = dict(items[0])
        entry.update(mutations)
        items[0] = entry
        snap.items = items
        db.session.commit()
        db.session.expire_all()
        return ali_order

    def test_invalid_logistics_service_blocks(self, db, make_user, ae_supplier,
                                              ae_product, sp_variant_factory):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        self._inject_invalid_snapshot(db, ali_order, logistics_service_name="   ")
        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "ready"
        assert fake.calls == []
        assert "submission_snapshot_invalid" in result.aliexpress_order.last_error

    def test_invalid_sku_attr_blocks(self, db, make_user, ae_supplier,
                                     ae_product, sp_variant_factory):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        self._inject_invalid_snapshot(db, ali_order, sku_attr="")
        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "ready"
        assert fake.calls == []
        assert "submission_snapshot_invalid" in result.aliexpress_order.last_error

    def test_invalid_product_count_blocks(self, db, make_user, ae_supplier,
                                          ae_product, sp_variant_factory):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        self._inject_invalid_snapshot(db, ali_order, product_count=0)
        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "ready"
        assert fake.calls == []
        assert "submission_snapshot_invalid" in result.aliexpress_order.last_error

    def test_invalid_product_id_blocks(self, db, make_user, ae_supplier,
                                       ae_product, sp_variant_factory):
        _persist_valid_credential(db, ae_supplier)
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        ali_order, _, _ = _materialize_ready(
            db, make_user, ae_supplier, ae_product, variant
        )
        self._inject_invalid_snapshot(db, ali_order, product_id="")
        fake = FakeSubmitter()
        result = submit_aliexpress_order(ali_order, submitter=fake)
        assert result.state == "ready"
        assert fake.calls == []
        assert "submission_snapshot_invalid" in result.aliexpress_order.last_error