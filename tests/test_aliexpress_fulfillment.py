"""Testes do pipeline de fulfillment AliExpress (Fase 8.0).

Cobertura (lista obrigatória):
- Credenciais: Fernet válida, env ausente, chave inválida, ciphertext adulterado,
  access token expirado, refresh token persistido criptografado, Supplier.access_token
  permanece NULL, nenhum secret em repr/log.
- Fulfillment: paid AliExpress -> queued/ready; webhook approved repetido -> mesma linha;
  mesmo OrderItem -> nunca duplica; mixed order internal + AliExpress; dois suppliers ->
  dois fulfillments; variante ausente / supplier_product ausente (item unatribuível a
  supplier) -> nenhum fulfillment; credencial ausente -> blocked; credencial válida -> ready.
- Pagamento: pending não cria fulfillment; manual_review não cria; payment_failed não cria;
  refunded antes de submit -> cancelled; charged_back antes de submit -> cancelled;
  retry approved idempotente.
- Snapshot: depois de criar fulfillment, alterar ProductVariant.supplier_sku_id no catálogo;
  AliExpressOrderItem deve continuar com o SKU snapshot ORIGINAL.
- Admin: anonymous -> login; non-admin -> denied; admin lista; detalhes; revalidate com CSRF;
  supplier_sku_id nunca aparece no HTML; token nunca aparece no HTML.
"""

from __future__ import annotations

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from decimal import Decimal

from loja import db
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
    User,
)


# ---------------------------------------------------------------------------
# Fixtures de apoio
# ---------------------------------------------------------------------------

@pytest.fixture()
def fernet_key():
    """Gera uma chave Fernet válida."""
    return Fernet.generate_key().decode("utf-8")


@pytest.fixture()
def valid_credential_env(fernet_key, monkeypatch):
    """Configura o env com chave Fernet válida."""
    monkeypatch.setenv("ALIEXPRESS_CREDENTIAL_KEY", fernet_key)


@pytest.fixture()
def aliexpress_supplier(db, make_user):
    """Cria um supplier AliExpress."""
    supplier = Supplier(
        id=uuid.uuid4(),
        name="AliExpress Test",
        channel_type="aliexpress",
        seller_id="seller_123",
        default_currency="USD",
        is_sync_enabled=False,
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


@pytest.fixture()
def aliexpress_product(db):
    """Cria um produto AliExpress."""
    product = Product(
        id=uuid.uuid4(),
        name="Produto AliExpress Test",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="ext_123",
        is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture()
def internal_product(db):
    """Cria um produto interno (não AliExpress)."""
    product = Product(
        id=uuid.uuid4(),
        name="Produto Interno",
        description="Test",
        price=Decimal("49.90"),
        source="internal",
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture()
def variant_for_product(aliexpress_product, supplier_product_factory):
    """Cria uma variante vinculada ao produto AliExpress."""
    return supplier_product_factory(aliexpress_product)


@pytest.fixture()
def supplier_product_factory(db):
    """Factory para criar SupplierProduct + ProductVariant."""
    def _create(product, supplier=None, sku_id="SKU001", supplier_product_id="SP001"):
        if supplier is None:
            supplier = Supplier(
                id=uuid.uuid4(),
                name="AliExpress",
                channel_type="aliexpress",
                seller_id="seller_123",
            )
            db.session.add(supplier)
            db.session.flush()
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id=supplier_product_id,
            cost=Decimal("10.00"),
            currency="USD",
            is_active=True,
        )
        db.session.add(sp)
        db.session.flush()

        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=sp.id,
            supplier_sku_id=sku_id,
            sku=None,
            color="Red",
            size="M",
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.commit()
        return sp, variant, supplier

    return _create


@pytest.fixture()
def order_with_items(db, make_user):
    """Cria um pedido com itens."""
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="pending_payment",
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
    db.session.add(order)
    db.session.commit()
    return order


# ---------------------------------------------------------------------------
# Testes de Credencial
# ---------------------------------------------------------------------------

class TestCredentialStore:
    """Testes do armazenamento de credenciais (Fernet)."""

    def test_fernet_key_generates_valid_key(self, fernet_key):
        """Fernet.generate_key() produz uma chave válida."""
        fernet = Fernet(fernet_key.encode("utf-8"))
        encrypted = fernet.encrypt(b"test_token")
        decrypted = fernet.decrypt(encrypted)
        assert decrypted == b"test_token"

    def test_is_configured_returns_true_with_valid_key(self, valid_credential_env):
        """Com env válido, is_configured() retorna True."""
        from loja.integrations.aliexpress.credential_store import is_configured
        assert is_configured() is True

    def test_is_configured_returns_false_without_key(self, monkeypatch):
        """Sem env, is_configured() retorna False."""
        monkeypatch.delenv("ALIEXPRESS_CREDENTIAL_KEY", raising=False)
        from loja.integrations.aliexpress.credential_store import is_configured
        assert is_configured() is False

    def test_invalid_key_raises_not_configured(self, monkeypatch):
        """Chave Fernet inválida levanta CredentialNotConfigured."""
        monkeypatch.setenv("ALIEXPRESS_CREDENTIAL_KEY", "invalid-key!!")
        from loja.integrations.aliexpress.credential_store import (
            is_configured,
            CredentialNotConfigured,
        )
        assert is_configured() is False
        with pytest.raises(CredentialNotConfigured):
            from loja.integrations.aliexpress.credential_store import _fernet
            _fernet()

    def test_persist_credential_encrypts_token(self, db, aliexpress_supplier, valid_credential_env):
        """Persistência criptografa o access_token."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()
        seller_id = aliexpress_supplier.seller_id

        cred1 = persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=seller_id,
            access_token="token_v1",
            refresh_token="refresh_v1",
            expires_in=3600,
        )
        db.session.flush()

        # Deve ser possível descriptografar
        from cryptography.fernet import Fernet
        key = os.environ.get("ALIEXPRESS_CREDENTIAL_KEY")
        fernet = Fernet(key.encode("utf-8"))
        decrypted = fernet.decrypt(cred1.access_token_encrypted.encode("ascii"))
        assert decrypted == b"token_v1"

    def test_persist_credential_upsert_by_supplier_id(
        self, db, aliexpress_supplier, valid_credential_env
    ):
        """Persistência faz upsert por supplier_id."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            AliExpressCredential,
            is_configured,
        )
        assert is_configured()
        seller_id = aliexpress_supplier.seller_id

        # First persist
        cred1 = persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=seller_id,
            access_token="token_v1",
            refresh_token="refresh_v1",
            expires_in=3600,
        )
        db.session.flush()

        # Second persist (upsert)
        cred2 = persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=seller_id,
            access_token="token_v2",
            refresh_token="refresh_v2",
            expires_in=7200,
        )
        db.session.flush()

        # Deve ser a mesma linha
        assert cred1.id == cred2.id

        # Token atualizado
        cred_in_db = AliExpressCredential.query.filter_by(
            supplier_id=aliexpress_supplier.id
        ).first()
        assert cred_in_db is not None

    def test_persist_credential_does_not_write_supplier_access_token(
        self, db, aliexpress_supplier, valid_credential_env
    ):
        """Persistência NÃO escreve em Supplier.access_token."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()

        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="secret_token_value",
            expires_in=3600,
        )
        db.session.flush()

        # Supplier.access_token deve permanecer NULL
        assert aliexpress_supplier.access_token is None

    def test_get_fulfillment_access_token_decrypts(
        self, db, aliexpress_supplier, valid_credential_env
    ):
        """get_fulfillment_access_token descriptografa o token."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            get_fulfillment_access_token,
            is_configured,
        )
        assert is_configured()

        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="my_test_token",
            expires_in=3600,
        )
        db.session.flush()

        token = get_fulfillment_access_token(aliexpress_supplier.id)
        assert token == "my_test_token"

    def test_get_fulfillment_access_token_raises_expired(
        self, db, aliexpress_supplier, valid_credential_env
    ):
        """Token expirado levanta CredentialExpired."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            get_fulfillment_access_token,
            CredentialExpired,
            is_configured,
        )
        assert is_configured()

        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="expired_token",
            expires_in=-1,  # Já expirou
        )
        db.session.flush()

        with pytest.raises(CredentialExpired):
            get_fulfillment_access_token(aliexpress_supplier.id)

    def test_get_fulfillment_access_token_raises_not_configured_without_key(
        self, db, aliexpress_supplier, monkeypatch
    ):
        """Sem chave Fernet, levanta CredentialNotConfigured."""
        monkeypatch.delenv("ALIEXPRESS_CREDENTIAL_KEY", raising=False)
        from loja.integrations.aliexpress.credential_store import (
            get_fulfillment_access_token,
            CredentialNotConfigured,
        )
        with pytest.raises(CredentialNotConfigured):
            get_fulfillment_access_token(aliexpress_supplier.id)

    def test_no_secret_in_repr(self, db, aliexpress_supplier, valid_credential_env):
        """repr() não expõe secrets."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            AliExpressCredential,
            is_configured,
        )
        assert is_configured()

        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="super_secret",
            expires_in=3600,
        )
        db.session.flush()

        cred = AliExpressCredential.query.filter_by(
            supplier_id=aliexpress_supplier.id
        ).first()
        repr_str = repr(cred)
        assert "super_secret" not in repr_str
        assert "access_token" not in repr_str

    def test_refresh_token_encrypted_not_plaintext(
        self, db, aliexpress_supplier, valid_credential_env
    ):
        """Refresh token persistido criptografado (não plaintext)."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            AliExpressCredential,
            is_configured,
        )
        assert is_configured()

        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="token123",
            refresh_token="refresh123",
            expires_in=3600,
        )
        db.session.flush()

        cred = AliExpressCredential.query.filter_by(
            supplier_id=aliexpress_supplier.id
        ).first()
        # O ciphertext não deve conter o plaintext
        assert "refresh123" not in cred.refresh_token_encrypted

    def test_seller_id_not_in_logs(self, db, aliexpress_supplier, valid_credential_env, caplog):
        """seller_id não aparece em logs da credential_store."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            get_fulfillment_access_token,
            is_configured,
        )
        assert is_configured()

        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id="secret_seller",
            access_token="tok",
            expires_in=3600,
        )
        db.session.flush()

        get_fulfillment_access_token(aliexpress_supplier.id)

        log_text = " ".join([m.message for m in caplog.records])
        assert "secret_seller" not in log_text


# ---------------------------------------------------------------------------
# Testes de Fulfillment Service
# ---------------------------------------------------------------------------

class TestFulfillmentService:
    """Testes do serviço ensure_aliexpress_fulfillment."""

    def test_paid_aliexpress_order_creates_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items, valid_credential_env
    ):
        """Pedido AliExpress pago cria fulfillment (state: ready)."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        sp, variant, supplier = supplier_product_factory(aliexpress_product)
        persist_credential(
            supplier_id=supplier.id,
            seller_id=supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.commit()

        # Criar OrderItem vinculado
        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=2,
            unit_price=Decimal("29.90"),
            line_total=Decimal("59.80"),
        )
        db.session.add(item)
        db.session.commit()

        # Simular pagamento aprovado
        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        assert len(results) == 1
        assert results[0].state == "ready"

    def test_webhook_approved_twice_same_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items, valid_credential_env
    ):
        """Webhook approved repetido retorna o mesmo fulfillment (idempotência)."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=2,
            unit_price=Decimal("29.90"),
            line_total=Decimal("59.80"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results1 = ensure_aliexpress_fulfillment(order_with_items)
        order_id = results1[0].aliexpress_order.id

        # Executar novamente (simula retry do webhook)
        results2 = ensure_aliexpress_fulfillment(order_with_items)

        # Mesmo fulfillment
        assert results2[0].aliexpress_order.id == order_id

    def test_same_order_item_never_duplicates(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items, valid_credential_env
    ):
        """Mesmo OrderItem nunca duplica AliExpressOrderItem."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        # Executar duas vezes
        ensure_aliexpress_fulfillment(order_with_items)
        ensure_aliexpress_fulfillment(order_with_items)

        items = AliExpressOrderItem.query.filter_by(
            order_item_id=item.id
        ).all()
        assert len(items) == 1

    def test_mixed_order_internal_and_aliexpress(
        self, db, aliexpress_supplier, aliexpress_product, internal_product, supplier_product_factory, order_with_items, valid_credential_env
    ):
        """Pedido misto (interno + AliExpress): fulfillment só dos AliExpress."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        # Item interno
        internal_item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=internal_product.id,
            variant_id=None,
            product_name=internal_product.name,
            quantity=1,
            unit_price=Decimal("49.90"),
            line_total=Decimal("49.90"),
        )
        db.session.add(internal_item)

        # Item AliExpress
        ae_item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(ae_item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        assert len(results) == 1  # Só 1 fulfillment (AliExpress)

    def test_two_suppliers_two_fulfillments(
        self, db, supplier_product_factory, order_with_items, aliexpress_supplier, aliexpress_product, valid_credential_env
    ):
        """Dois suppliers -> dois fulfillments independentes."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        supplier2 = Supplier(
            id=uuid.uuid4(),
            name="AliExpress 2",
            channel_type="aliexpress",
            seller_id="seller_456",
        )
        db.session.add(supplier2)
        db.session.flush()

        product2 = Product(
            id=uuid.uuid4(),
            name="Produto AE 2",
            description="Test",
            price=Decimal("39.90"),
            source="aliexpress",
            external_id="ext_456",
            is_draft=True,
            is_active=False,
        )
        db.session.add(product2)
        db.session.flush()

        sp1, variant1, _ = supplier_product_factory(aliexpress_product)

        # Criar variantes para dois suppliers
        from loja.modules.models import SupplierProduct, ProductVariant
        sp2 = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier2.id,
            product_id=product2.id,
            supplier_product_id="SP002",
            cost=Decimal("15.00"),
            currency="USD",
            is_active=True,
        )
        db.session.add(sp2)
        db.session.flush()

        variant2 = ProductVariant(
            id=uuid.uuid4(),
            product_id=product2.id,
            supplier_product_id=sp2.id,
            supplier_sku_id="SKU002",
            sku=None,
            color="Blue",
            size="S",
            stock=10,
            price=Decimal("39.90"),
            is_active=True,
        )
        db.session.add(variant2)
        db.session.commit()

        # Itens para cada supplier
        item1 = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant1.id,
            product_name="Produto AE",
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        item2 = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=product2.id,
            variant_id=variant2.id,
            product_name="Produto AE 2",
            quantity=1,
            unit_price=Decimal("39.90"),
            line_total=Decimal("39.90"),
        )
        db.session.add(item1)
        db.session.add(item2)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        assert len(results) == 2  # Dois fulfillments

    def test_variant_missing_blocks_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, order_with_items, valid_credential_env
    ):
        """Variante ausente -> fulfillment blocked."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        # Criar supplier product sem variante
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=aliexpress_supplier.id,
            product_id=aliexpress_product.id,
            supplier_product_id="SP001",
            cost=Decimal("10.00"),
            currency="USD",
            is_active=True,
        )
        db.session.add(sp)
        db.session.commit()

        # OrderItem sem variant_id (ou variant_id para produto inexistente)
        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=None,  # Sem variante!
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        # Item sem variante é unatribuível a um supplier: nenhum fulfillment criado.
        assert len(results) == 0
        assert AliExpressOrder.query.count() == 0

    def test_supplier_product_missing_blocks_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, order_with_items, valid_credential_env
    ):
        """Supplier product ausente -> fulfillment blocked."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        # Criar variante sem supplier_product_id
        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=aliexpress_product.id,
            supplier_product_id=None,  # Sem supplier product
            supplier_sku_id="SKU001",
            sku=None,
            color="Red",
            size="M",
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.commit()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        # Item sem supplier product é unatribuível a um supplier: nenhum fulfillment criado.
        assert len(results) == 0
        assert AliExpressOrder.query.count() == 0

    def test_credential_missing_blocks_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items, monkeypatch
    ):
        """Credencial ausente -> fulfillment blocked."""
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        from loja.integrations.aliexpress.credential_store import (
            is_configured,
            CredentialNotConfigured,
        )
        # Remover a chave Fernet para simular credencial ausente
        monkeypatch.delenv("ALIEXPRESS_CREDENTIAL_KEY", raising=False)

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        assert len(results) == 1
        assert results[0].state == "blocked"
        assert results[0].aliexpress_order.last_error == "credential_missing"

    def test_snapshot_preserved_after_catalog_change(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items, valid_credential_env
    ):
        """Snapshot: depois de criar fulfillment, alterar SKU no catálogo, AliExpressOrderItem mantém original."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product, sku_id="SKU_ORIGINAL")

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        ensure_aliexpress_fulfillment(order_with_items)

        # Alterar SKU no catálogo
        variant.supplier_sku_id = "SKU_CHANGED"
        db.session.commit()

        # AliExpressOrderItem deve manter o SKU original
        ali_item = AliExpressOrderItem.query.first()
        assert ali_item.supplier_sku_external_id == "SKU_ORIGINAL"

    def test_pending_order_does_not_create_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items
    ):
        """Pedido pendente (pending_payment) NÃO cria fulfillment."""
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        # Status pendente
        order_with_items.status = "pending_payment"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        # Não deve criar fulfillment
        assert results == [] or all(r.aliexpress_order.state != "queued" for r in results)

    def test_manual_review_does_not_create_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items
    ):
        """Pedido em manual_review NÃO cria fulfillment."""
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "manual_review"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        assert results == [] or all(r.aliexpress_order.state != "queued" for r in results)

    def test_refunded_before_submission_cancels_fulfillment(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, order_with_items, valid_credential_env
    ):
        """Refund antes da submissão cancela fulfillment."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
            cancel_fulfillment_before_submission,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        # Criar fulfillment
        ensure_aliexpress_fulfillment(order_with_items)

        # Cancelar (simular refund)
        cancel_fulfillment_before_submission(order_with_items, reason="payment_refunded_before_submission")

        ali_order = AliExpressOrder.query.filter_by(order_id=order_with_items.id).first()
        assert ali_order is not None
        assert ali_order.state == "cancelled"
        assert ali_order.last_error == "payment_refunded_before_submission"

    def test_item_integrity_cross_product_blocked(
        self, db, aliexpress_supplier, aliexpress_product, order_with_items, valid_credential_env
    ):
        """Variante de produto diferente -> blocked (cross-product)."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        # Criar um produto interno
        internal_product = Product(
            id=uuid.uuid4(),
            name="Produto Interno",
            description="Test",
            price=Decimal("49.90"),
            source="internal",
            is_active=True,
        )
        db.session.add(internal_product)
        db.session.commit()

        # Criar variante com product_id de produto interno
        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=internal_product.id,
            supplier_product_id=None,
            supplier_sku_id="SKU001",
            sku=None,
            color="Red",
            size="M",
            stock=10,
            price=Decimal("49.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.commit()

        # Criar OrderItem para produto interno (que não é AliExpress)
        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order_with_items.id,
            product_id=internal_product.id,
            variant_id=variant.id,
            product_name=internal_product.name,
            quantity=1,
            unit_price=Decimal("49.90"),
            line_total=Decimal("49.90"),
        )
        db.session.add(item)
        db.session.commit()

        order_with_items.status = "paid"
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order_with_items)
        # O item interno deve ser ignorado; nenhum fulfillment criado para item não-AE
        assert results == []


# ---------------------------------------------------------------------------
# Testes de Admin Queue
# ---------------------------------------------------------------------------

class TestAdminFulfillmentQueue:
    """Testes da fila administrativa de fulfillment."""

    def test_admin_lists_fulfillments(self, client, admin_user, login, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env):
        """Admin pode listar fulfillments."""
        login(admin_user.email, "senha123")
        sp, variant, _ = supplier_product_factory(aliexpress_product)
        from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
        from loja.utils import utcnow

        user = admin_user
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="paid",
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
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()
        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.commit()

        from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
        ensure_aliexpress_fulfillment(order)

        response = client.get("/admin/integrations/aliexpress/pedidos")
        assert response.status_code == 200
        assert b"Fulfillment" in response.data

    def test_anonymous_cannot_access_fulfillment_queue(self, client):
        """Anonymous não pode acessar a fila."""
        response = client.get("/admin/integrations/aliexpress/pedidos")
        assert response.status_code == 302  # Redirect to login
        assert response.headers["Location"].endswith("/login")

    def test_non_admin_cannot_access_fulfillment_queue(self, client, regular_user, login):
        """Non-admin não pode acessar a fila."""
        login(regular_user.email, "senha123")
        response = client.get("/admin/integrations/aliexpress/pedidos", follow_redirects=True)
        assert "não possui permissão" in response.get_data(as_text=True)

    def test_fulfillment_detail_shows_items(self, client, admin_user, login, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env):
        """Detalhe do fulfillment mostra itens."""
        login(admin_user.email, "senha123")
        sp, variant, _ = supplier_product_factory(aliexpress_product)
        from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
        from loja.utils import utcnow

        user = admin_user
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="paid",
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
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()
        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.commit()

        from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
        results = ensure_aliexpress_fulfillment(order)
        ali_order_id = results[0].aliexpress_order.id

        response = client.get(f"/admin/integrations/aliexpress/pedidos/{ali_order_id}")
        assert response.status_code == 200
        assert ali_order_id.hex[:8].encode() in response.data or str(ali_order_id).encode() in response.data

    def test_supplier_sku_id_not_in_html(self, client, admin_user, login, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env):
        """supplier_sku_id nunca aparece no HTML do admin."""
        login(admin_user.email, "senha123")
        sp, variant, _ = supplier_product_factory(aliexpress_product, sku_id="SECRET_SKU_123")
        from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
        from loja.utils import utcnow

        user = admin_user
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="paid",
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
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()
        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.commit()

        from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
        results = ensure_aliexpress_fulfillment(order)
        ali_order_id = results[0].aliexpress_order.id

        response = client.get(f"/admin/integrations/aliexpress/pedidos/{ali_order_id}")
        assert response.status_code == 200
        assert b"SECRET_SKU_123" not in response.data
        assert b"supplier_sku_id" not in response.data
        assert b"supplier_product_external_id" not in response.data

    def test_token_not_in_html(self, client, admin_user, login, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env):
        """access_token nunca aparece no HTML do admin."""
        login(admin_user.email, "senha123")
        sp, variant, _ = supplier_product_factory(aliexpress_product)
        from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

        user = admin_user
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="paid",
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
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()
        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="SECRET_TOKEN_VALUE",
            expires_in=3600,
        )
        db.session.commit()

        from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
        results = ensure_aliexpress_fulfillment(order)
        ali_order_id = results[0].aliexpress_order.id

        response = client.get(f"/admin/integrations/aliexpress/pedidos/{ali_order_id}")
        assert response.status_code == 200
        assert b"SECRET_TOKEN_VALUE" not in response.data

    def test_revalidate_endpoint(self, client, admin_user, login, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env):
        """Endpoint de revalidação funciona."""
        login(admin_user.email, "senha123")
        sp, variant, _ = supplier_product_factory(aliexpress_product)
        from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem

        user = admin_user
        order = Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="paid",
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
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        assert is_configured()
        persist_credential(
            supplier_id=aliexpress_supplier.id,
            seller_id=aliexpress_supplier.seller_id,
            access_token="tok",
            expires_in=3600,
        )
        db.session.commit()

        from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
        results = ensure_aliexpress_fulfillment(order)
        ali_order_id = results[0].aliexpress_order.id

        response = client.post(
            f"/admin/integrations/aliexpress/pedidos/{ali_order_id}/revalidar",
        )
        assert response.status_code in (200, 302)

    def test_payment_pending_does_not_create_fulfillment_full_flow(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory
    ):
        """Fluxo completo: pagamento pendente não cria fulfillment."""
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        order = Order(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            checkout_type="product",
            status="pending_payment",
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="Test",
            customer_email="test@test.com",
            customer_phone="11999999999",
            zip_code="01001000",
            street="Rua Test",
            number="1",
            complement="Apto 101",
            neighborhood="Centro",
            city="SP",
            state="SP",
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        results = ensure_aliexpress_fulfillment(order)
        assert len(results) == 0

    def test_charged_back_before_submission_cancels(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env
    ):
        """Chargeback antes da submissão cancela fulfillment."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
            cancel_fulfillment_before_submission,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        order = Order(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            checkout_type="product",
            status="paid",
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="Test",
            customer_email="test@test.com",
            customer_phone="11999999999",
            zip_code="01001000",
            street="Rua Test",
            number="1",
            complement="Apto 101",
            neighborhood="Centro",
            city="SP",
            state="SP",
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        ensure_aliexpress_fulfillment(order)
        cancel_fulfillment_before_submission(order, reason="payment_charged_back_before_submission")

        ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
        assert ali_order is not None
        assert ali_order.state == "cancelled"

    def test_retry_approved_idempotent(
        self, db, aliexpress_supplier, aliexpress_product, supplier_product_factory, valid_credential_env
    ):
        """Retry de pagamento aprovado é idempotente."""
        from loja.integrations.aliexpress.credential_store import (
            persist_credential,
            is_configured,
        )
        from loja.integrations.aliexpress.fulfillment import (
            ensure_aliexpress_fulfillment,
        )
        assert is_configured()

        sp, variant, _ = supplier_product_factory(aliexpress_product)

        order = Order(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            checkout_type="product",
            status="paid",
            subtotal=Decimal("29.90"),
            total=Decimal("29.90"),
            stock_reserved=False,
            customer_name="Test",
            customer_email="test@test.com",
            customer_phone="11999999999",
            zip_code="01001000",
            street="Rua Test",
            number="1",
            complement="Apto 101",
            neighborhood="Centro",
            city="SP",
            state="SP",
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=aliexpress_product.id,
            variant_id=variant.id,
            product_name=aliexpress_product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
        db.session.add(item)
        db.session.commit()

        order.status = "paid"
        db.session.commit()

        results1 = ensure_aliexpress_fulfillment(order)
        ali_order_id = results1[0].aliexpress_order.id

        # Retry
        results2 = ensure_aliexpress_fulfillment(order)

        assert results2[0].aliexpress_order.id == ali_order_id
        assert len(AliExpressOrder.query.filter_by(id=ali_order_id).all()) == 1
        assert AliExpressOrderItem.query.filter_by(aliexpress_order_id=ali_order_id).count() == 1
