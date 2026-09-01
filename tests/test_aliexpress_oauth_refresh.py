"""Testes de renovação OAuth AliExpress (Fase 8.0.4) — HARD-OFF de criação.

Garante que:
- boundary explícito ``AliExpressOAuthRefresher`` (domínio nunca fala HTTP de
  renovação diretamente);
- provider padrão é hard-off (``HardOffOAuthRefresher``), sem config ausente
  que dispare chamada externa acidental;
- refresh é síncrono, explícito, atômico e idempotente (já válida não chama
  provider; duas chamadas sequenciais = um refresh lógico);
- rotação: provider devolve novo refresh_token -> persiste; não devolve ->
  preserva o atual;
- falha/rejeição/malformada mantém credencial e refresh_token íntegros e
  persiste apenas erro sanitizado (nunca tokens/secrets em logs/caplog);
- integração: integridade prevalece sobre credencial (item_integrity_error
  não chama refresher); refresh nunca altera snapshot nem
  ``ali_express_order_id``; catálogo pós-snapshot continua irrelevante;
- criação real continua proibida: token renovado não libera submission real.
"""
import logging
import socket
import uuid
from decimal import Decimal

import pytest

from datetime import timedelta

from loja import db
from loja.integrations.aliexpress.credential_store import (
    AliExpressCredential,
    CredentialExpired,
    _decrypt_or_raise,
    get_fulfillment_access_token,
    persist_credential,
)
from loja.integrations.aliexpress.fulfillment import (
    ERROR_CODES,
    ensure_aliexpress_fulfillment,
)
from loja.integrations.aliexpress.oauth_refresh import (
    AliExpressCredentialRefreshResult,
    AliExpressOAuthRefreshError,
    AliExpressOAuthRefresher,
    AliExpressOAuthRefreshRejected,
    AliExpressOAuthRefreshRequest,
    AliExpressOAuthRefreshResult,
    AliExpressOAuthRefreshUnavailable,
    HardOffOAuthRefresher,
    OAUTH_CODES,
    get_default_oauth_refresher,
    refresh_aliexpress_credential,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
    HardOffOrderSubmitter,
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
from loja.utils import utcnow


@pytest.fixture(autouse=True)
def _valid_credential_env(monkeypatch):
    """Credencial e criptografia funcional p/ persistir tokens nos testes."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    """Bloqueia qualquer conexão de rede durante os testes da Fase 8.0.4."""

    def _reject(*args, **kwargs):
        raise AssertionError(
            "tentativa de conexão de rede durante teste da Fase 8.0.4"
        )

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class FakeRefresher(AliExpressOAuthRefresher):
    """Provider de refresh de teste instrumentado (nunca faz rede)."""

    def __init__(
        self,
        *,
        access_token="TOKEN_NOVO",
        expires_in=3600,
        refresh_token=None,
        refresh_expires_in=None,
        exc=None,
        malformed=False,
    ):
        self.calls = []
        self.access_token = access_token
        self.expires_in = expires_in
        self.refresh_token = refresh_token
        self.refresh_expires_in = refresh_expires_in
        self.exc = exc
        self.malformed = malformed

    def refresh(self, request: AliExpressOAuthRefreshRequest):
        self.calls.append(request)
        if self.malformed:
            return None
        if self.exc is not None:
            raise self.exc
        return AliExpressOAuthRefreshResult(
            access_token=self.access_token,
            expires_in=self.expires_in,
            refresh_token=self.refresh_token,
            refresh_expires_in=self.refresh_expires_in,
        )


class _FakeSubmitter(AliExpressOrderSubmitter):
    """Provider de submission fake p/ provar que submissão após refresh usa
    a MESMA ordem/submit, sem rede real."""

    def __init__(self):
        self.calls = []

    def submit(self, request):
        self.calls.append(request)
        return AliExpressSubmissionResult(
            external_order_id=f"AE-OAUTH-{request.aliexpress_order_id.hex[:12]}"
        )


@pytest.fixture()
def ae_supplier(db):
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp OAuth 8.0.4",
        channel_type="aliexpress",
        seller_id="SELLER_OAUTH_804",
        default_currency="USD",
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


@pytest.fixture()
def ae_product(db):
    product = Product(
        id=uuid.uuid4(),
        name="Prod OAuth 8.0.4",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="EXT_OAUTH_804",
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
        sku="SKU_OAUTH",
        sp_id="SP_OAUTH",
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
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.commit()
        return sp, variant

    return _create


def _persist_credential(
    db, supplier, *, access_token="TOKEN_ANTIGO", refresh_token=None, expires_in=3600
):
    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    db.session.commit()


def _make_paid_order_with_item(
    db, make_user, product, variant, *, total=Decimal("29.90")
):
    """Pedido pago com um OrderItem (product_id/variant) e métricas de
    estruturas do fluxo fulfillment."""
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


def _snapshot(db, order):
    ali = AliExpressOrder.query.filter_by(order_id=order.id).all()
    items = AliExpressOrderItem.query.filter_by(aliexpress_order_id=ali[0].id).all()
    return ali[0], items


class TestRefreshBoundary:
    def test_credential_valida_nao_chama_refresher(
        self, db, ae_supplier
    ):
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=3600
        )
        fake = FakeRefresher()
        outcome = refresh_aliexpress_credential(
            ae_supplier.id, refresher=fake
        )
        assert outcome.ok
        assert not outcome.refreshed
        assert fake.calls == []

    def test_default_refresher_e_hard_off_sem_rede(self, db, ae_supplier):
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        default = get_default_oauth_refresher()
        assert isinstance(default, HardOffOAuthRefresher)

        outcome = refresh_aliexpress_credential(ae_supplier.id)
        assert not outcome.ok
        assert outcome.error_code == OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]

        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_VALIDO"
        assert cred.last_error == OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]


class TestRefreshSuccess:
    def test_expirada_com_refresh_valido_sucesso_atualiza_token_e_expiration(
        self, db, ae_supplier
    ):
        _persist_credential(
            db, ae_supplier,
            access_token="TOKEN_ANTIGO", refresh_token="RT_OLD", expires_in=0,
        )
        fake = FakeRefresher(access_token="TOKEN_NOVO")
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert outcome.ok
        assert outcome.refreshed
        assert len(fake.calls) == 1

        assert get_fulfillment_access_token(ae_supplier.id) == "TOKEN_NOVO"
        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert cred.status == "active"
        assert cred.access_token_expires_at > utcnow()
        assert cred.last_error is None
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_OLD"

    def test_refresh_token_rotacionado_e_atualizado(self, db, ae_supplier):
        _persist_credential(
            db, ae_supplier,
            access_token="TOKEN_ANTIGO", refresh_token="RT_OLD", expires_in=0,
        )
        fake = FakeRefresher(
            access_token="TOKEN_1", refresh_token="RT_NOVO", refresh_expires_in=7200
        )
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert outcome.ok
        assert outcome.refreshed
        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_NOVO"
        assert cred.refresh_token_expires_at is not None
        left = cred.refresh_token_expires_at - utcnow()
        assert 7100 < left.total_seconds() < 7300

        # 2ª renovação usa o NOVO refresh token (rotação de fato).
        cred.access_token_expires_at = utcnow() - timedelta(seconds=1)
        db.session.add(cred)
        db.session.commit()
        fake2 = FakeRefresher(access_token="TOKEN_2")
        outcome2 = refresh_aliexpress_credential(
            ae_supplier.id, refresher=fake2
        )
        assert outcome2.ok
        assert fake2.calls[0].refresh_token == "RT_NOVO"
        cred2 = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert _decrypt_or_raise(cred2.refresh_token_encrypted) == "RT_NOVO"

    def test_provider_sem_novo_refresh_token_preserva_atual(self, db, ae_supplier):
        _persist_credential(
            db, ae_supplier,
            access_token="TOKEN_ANTIGO",
            refresh_token="RT_SOBREVIVENTE", expires_in=0,
        )
        fake = FakeRefresher(refresh_token=None)
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert outcome.ok
        assert outcome.refreshed
        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_SOBREVIVENTE"

    def test_idempotencia_duas_chamadas_sequenciais_um_refresh(
        self, db, ae_supplier
    ):
        _persist_credential(
            db, ae_supplier, refresh_token="RT_OLD", expires_in=0
        )
        fake = FakeRefresher()
        r1 = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert r1.ok
        assert r1.refreshed
        r2 = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert r2.ok
        assert not r2.refreshed
        assert len(fake.calls) == 1


class TestRefreshBlocked:
    def test_ausencia_de_refresh_token_mantem_bloqueio(self, db, ae_supplier):
        _persist_credential(db, ae_supplier, expires_in=0)
        fake = FakeRefresher()
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert not outcome.ok
        assert outcome.error_code == OAUTH_CODES["CREDENTIAL_EXPIRED"]
        assert fake.calls == []
        with pytest.raises(CredentialExpired):
            get_fulfillment_access_token(ae_supplier.id)

    def test_refresh_rejeitado_mantem_bloqueio_e_sanitiza(
        self, db, ae_supplier, caplog
    ):
        _persist_credential(
            db, ae_supplier, refresh_token="RT_OLD", expires_in=0
        )
        caplog.set_level(
            logging.WARNING, logger="loja.integrations.aliexpress.oauth_refresh"
        )
        fake = FakeRefresher(
            exc=AliExpressOAuthRefreshRejected(
                "refresh_token invalido secret=SUPER_SECRET_123"
            )
        )
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert not outcome.ok
        assert outcome.error_code == OAUTH_CODES["CREDENTIAL_REFRESH_REJECTED"]
        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_OLD"
        assert "SUPER_SECRET_123" not in caplog.text

    def test_resposta_malformada_e_indisponibilidade_mantem_bloqueio(
        self, db, ae_supplier
    ):
        _persist_credential(
            db, ae_supplier, refresh_token="RT_OLD", expires_in=0
        )
        fake = FakeRefresher(malformed=True)
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert not outcome.ok
        assert outcome.error_code == OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_OLD"
        assert cred.last_error == OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        with pytest.raises(CredentialExpired):
            get_fulfillment_access_token(ae_supplier.id)

    def test_falha_nao_inventa_credential_valida(self, db, ae_supplier):
        _persist_credential(
            db, ae_supplier, refresh_token="RT_OLD", expires_in=0
        )
        fake = FakeRefresher(
            exc=AliExpressOAuthRefreshUnavailable("fora do ar")
        )
        outcome = refresh_aliexpress_credential(ae_supplier.id, refresher=fake)
        assert not outcome.ok
        assert outcome.error_code == OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        cred = AliExpressCredential.query.filter_by(
            supplier_id=ae_supplier.id
        ).first()
        assert cred is not None
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_OLD"
        with pytest.raises(CredentialExpired):
            # Nenhum token fake foi inventado na falha.
            get_fulfillment_access_token(ae_supplier.id)


class TestOAuthFulfillmentIntegration:
    def test_refresh_nao_altera_snapshot_nem_aliexpress_order_id(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)

        r1 = ensure_aliexpress_fulfillment(order)
        assert r1[0].state == "blocked"
        assert "credential_expired" in r1[0].aliexpress_order.last_error
        db.session.commit()
        snap_before, items_before = _snapshot(db, order)
        ali_id_before = snap_before.ali_order_id

        fake = FakeRefresher()
        r2 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r2[0].state == "ready"
        assert len(fake.calls) == 1
        snap_after, items_after = _snapshot(db, order)
        assert snap_after.id == snap_before.id
        assert snap_after.ali_order_id == ali_id_before is None
        assert len(items_after) == len(items_before)

    def test_aliexpress_order_id_estavel_apos_reenvio_com_refresh(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)

        r1 = ensure_aliexpress_fulfillment(order)
        assert r1[0].state == "blocked"
        db.session.commit()

        fake = FakeRefresher()
        r2 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r2[0].state == "ready"
        r3 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r3[0].state == "ready"
        assert r3[0].aliexpress_order.id == r2[0].aliexpress_order.id
        assert len(fake.calls) == 1
        assert AliExpressOrder.query.filter_by(order_id=order.id).count() == 1

    def test_item_integrity_error_prevalece_mesmo_com_refresh_sucesso(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        product_b = Product(
            id=uuid.uuid4(),
            name="Prod B 8.0.4",
            description="Test",
            price=Decimal("29.90"),
            source="aliexpress",
            external_id="EXT_OAUTH_B",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product_b)
        db.session.commit()
        sp, variant_b = sp_variant_factory(ae_supplier, product_b)
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        order = Order(
            id=uuid.uuid4(),
            user_id=make_user().id,
            checkout_type="product",
            status="paid",
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="U",
            customer_email="u@oauth.test",
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
        # OrderItem referencia PRODUTO A com variante do PRODUTO B.
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

        fake = FakeRefresher()
        r1 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r1[0].state == "blocked"
        assert ERROR_CODES["ITEM_INTEGRITY_ERROR"] in r1[0].aliexpress_order.last_error
        # Precedência: integridade vem antes da credencial; refresher nem é
        # chamado mesmo com refresh disponível e válido.
        assert fake.calls == []
        assert r1[0].aliexpress_order.ali_order_id is None

    def test_catalogo_modificado_apos_snapshot_continua_irrelevante(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(
            ae_supplier, ae_product, sku="SKU_ORIGINAL", sp_id="SP_ORIGINAL"
        )
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)

        fake = FakeRefresher()
        r1 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r1[0].state == "ready"
        db.session.commit()
        snap, items_1 = _snapshot(db, order)
        ali_id = snap.ali_order_id

        # Catálogo muda DEPOIS do snapshot: irrelevante pro fulfillment.
        variant.supplier_sku_id = "SKU_NOVO"
        sp.supplier_product_id = "SP_NOVO"
        variant.price = Decimal("999.00")
        db.session.commit()

        r2 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r2[0].state == "ready"
        snap2, items_2 = _snapshot(db, order)
        assert snap2.id == snap.id
        assert snap2.ali_order_id == ali_id
        assert len(items_2) == len(items_1)
        item_snap = items_2[0]
        assert item_snap.supplier_sku_external_id == "SKU_ORIGINAL"
        assert item_snap.supplier_product_external_id == "SP_ORIGINAL"
        assert item_snap.supplier_unit_cost == Decimal("10.00")
        assert len(fake.calls) == 1

    def test_hard_off_de_criacao_continua_apos_refresh(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)

        fake = FakeRefresher()
        r1 = ensure_aliexpress_fulfillment(order, refresher=fake)
        assert r1[0].state == "ready"
        db.session.commit()

        from loja.integrations.aliexpress.submission import get_default_order_submitter

        assert isinstance(get_default_order_submitter(), HardOffOrderSubmitter)
        result = submit_aliexpress_order(r1[0].aliexpress_order)
        assert result.state == "submitting"
        assert (
            ERROR_CODES["SUBMISSION_HARD_OFF"]
            in result.aliexpress_order.last_error
        )


class TestOAuthSubmissionGate:
    def test_credencial_valida_submission_nao_chama_refresher(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=3600
        )
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)

        fake_ref = FakeRefresher()
        r = ensure_aliexpress_fulfillment(order, refresher=fake_ref)
        assert r[0].state == "ready"
        assert fake_ref.calls == []
        db.session.commit()

        fake_sub = _FakeSubmitter()
        result = submit_aliexpress_order(
            r[0].aliexpress_order, submitter=fake_sub
        )
        assert result.state == "submitted"
        assert len(fake_sub.calls) == 1

    def test_submission_apos_refresh_usando_mesma_ordem(
        self, db, make_user, ae_supplier, ae_product, sp_variant_factory
    ):
        sp, variant = sp_variant_factory(ae_supplier, ae_product)
        _persist_credential(
            db, ae_supplier, refresh_token="RT_VALIDO", expires_in=0
        )
        order = _make_paid_order_with_item(db, make_user, ae_product, variant)

        r1 = ensure_aliexpress_fulfillment(order)
        assert r1[0].state == "blocked"
        db.session.commit()

        fake_ref = FakeRefresher()
        r2 = ensure_aliexpress_fulfillment(order, refresher=fake_ref)
        assert r2[0].state == "ready"
        assert len(fake_ref.calls) == 1
        db.session.commit()

        fake_sub = _FakeSubmitter()
        result = submit_aliexpress_order(
            r2[0].aliexpress_order, submitter=fake_sub
        )
        assert result.state == "submitted"
        assert len(fake_sub.calls) == 1
        ali = AliExpressOrder.query.filter_by(order_id=order.id).first()
        assert ali.ali_order_id.startswith("AE-OAUTH-")
        assert fake_ref.calls[0].refresh_token == "RT_VALIDO"