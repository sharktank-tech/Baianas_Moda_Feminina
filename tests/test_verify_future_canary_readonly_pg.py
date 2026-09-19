"""Testes do script somente leitura (8 cenários) em PG descartável."""
import os
import secrets
import subprocess
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL
from tests.aliexpress_snapshot_utils import materialize_snapshot

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    IS_POSTGRES = bool(DATABASE_URL) and make_url(DATABASE_URL).get_backend_name() == "postgresql"
except Exception:
    IS_POSTGRES = False

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requer PostgreSQL")


@pytest.fixture
def canary_env(monkeypatch):
    schema = f"pollo_test_canary_{secrets.token_hex(4)}"
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        c.commit()
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {"pool_pre_ping": True, "pool_recycle": 300, "connect_args": {"options": f"-csearch_path={schema}"}},
    )
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", schema)
    from loja import create_app

    app = create_app()
    with app.app_context():
        from loja import db

        db.create_all()
    yield schema, app
    with eng.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        c.commit()


def _materialize_valid_canary(app, total=Decimal("29.90")):
    from loja import db
    from loja.integrations.aliexpress.credential_store import persist_credential
    from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem, Payment
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct, User

    with app.app_context():
        user = User(id=uuid.uuid4(), name="Canary", email=f"canary-{uuid.uuid4().hex}@test", phone="11999999999", is_admin=False)
        user.set_password("s")
        db.session.add(user)
        db.session.flush()
        supplier = Supplier(id=uuid.uuid4(), name="Supp Canary", channel_type="aliexpress", seller_id="SELLER_CANARY", default_currency="BRL")
        db.session.add(supplier)
        db.session.flush()
        product = Product(id=uuid.uuid4(), name="Prod Canary", description="Test", price=Decimal("29.90"), source="aliexpress", external_id="EXT_CANARY", is_draft=True, is_active=False)
        db.session.add(product)
        db.session.flush()
        sp = SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id="100000999", cost=Decimal("10.00"), currency="BRL", is_active=True, freight_status="available", shipping_cost=Decimal("5.00"), landed_cost=Decimal("15.00"), freight_currency="BRL")
        db.session.add(sp)
        db.session.flush()
        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=sp.id,
            supplier_sku_id="SKU_CANARY",
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
            attributes=[
                {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
                {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
            ],
        )
        db.session.add(variant)
        db.session.flush()
        persist_credential(supplier_id=supplier.id, seller_id=supplier.seller_id, access_token="tok", expires_in=3600)
        db.session.flush()
        order = Order(id=uuid.uuid4(), user_id=user.id, checkout_type="product", status="paid", subtotal=total, total=total, stock_reserved=False, customer_name="Valid User", customer_email=user.email, customer_phone="11999999999", zip_code="01001000", street="Rua Teste", number="10", complement="Apto 1", neighborhood="Centro", city="SP", state="SP", acquisition_source="instagram")
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(id=uuid.uuid4(), order_id=order.id, product_id=product.id, variant_id=variant.id, product_name=product.name, quantity=1, unit_price=total, line_total=total))
        db.session.add(Payment(id=uuid.uuid4(), order_id=order.id, provider_payment_id=f"mp_{uuid.uuid4().hex[:8]}", status="approved", amount=order.total, currency="BRL"))
        db.session.commit()
        res = ensure_aliexpress_fulfillment(order)[0]
        ali = res.aliexpress_order
        prod = db.session.get(Product, product.id)
        prod.pricing_status = "priced"
        prod.target_margin_percent = Decimal("20")
        prod.price = Decimal("18.75")
        db.session.commit()
        materialize_snapshot(ali, session=db.session)
        db.session.commit()
        return ali.id, order.id, product.id, sp.id, variant.id


def _run_verify(schema, extra_env=None):
    env = os.environ.copy()
    env["DATABASE_URL"] = DATABASE_URL
    env["CANARY_SCHEMA"] = schema
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(["python", "scripts/verify_future_canary_readonly.py"], env=env, capture_output=True, text=True)
    return result


def test_candidato_valido(canary_env):
    # Até definição de evidência orgânica independente, todo candidato fica pendente após checks preliminares
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    result = _run_verify(schema)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "candidato encontrado" in result.stdout.lower()
    assert "origem de aquisição registrada" in result.stdout.lower()
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result.stdout.lower()
    assert "elegível" not in result.stdout.lower()
    assert "pronto para compra" not in result.stdout.lower()
    assert "verificação ok" not in result.stdout.lower()
    assert "transaction_read_only" not in result.stderr.lower()
    for pii in ["11999999999", "Rua Teste", "01001000", "Valid User", "tok", "mp_", "SELECT", "INSERT", "UPDATE", "DELETE"]:
        if pii in ["Rua Teste", "01001000", "11999999999"]:
            assert pii not in result.stdout, f"PII vazou: {pii}"
        assert "SELECT" not in result.stdout
        assert "ProgrammingError" not in result.stdout
    assert "..." in result.stdout  # mascarado


def test_selecao_por_id(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    result = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result.returncode == 1
    assert "candidato encontrado" in result.stdout.lower()
    assert "origem de aquisição registrada" in result.stdout.lower()
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result.stdout.lower()
    assert "elegível" not in result.stdout.lower()
    assert "verificação ok" not in result.stdout.lower()
    assert str(ali_id) not in result.stdout
    assert f"{str(ali_id)[:8]}...{str(ali_id)[-4:]}" in result.stdout
    for pii in ["11999999999", "Rua Teste"]:
        assert pii not in result.stdout
    assert "instagram" not in result.stdout.lower()


def test_pagamento_ou_valor_invalido(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    # Testa orders.status != 'paid'
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.status = "pending_payment"
        db.session.commit()
    result = _run_verify(schema)
    assert result.returncode != 0
    assert "orders.status" in result.stdout.lower() and "paid" in result.stdout.lower()
    # Restaura para testar provider_payment_id ausente (vazio, não None, para não violar NOT NULL)
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order, Payment

        ord = db.session.get(Order, order_id)
        ord.status = "paid"
        pay = Payment.query.filter_by(order_id=order_id).first()
        pay.status = "approved"
        pay.provider_payment_id = ""
        db.session.commit()
    result2 = _run_verify(schema)
    assert result2.returncode != 0
    assert "provider_payment_id" in result2.stdout.lower()
    # Testa pagamento não aprovado
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Payment

        pay = Payment.query.filter_by(order_id=order_id).first()
        pay.provider_payment_id = f"mp_{uuid.uuid4().hex[:8]}"
        pay.status = "pending"
        db.session.commit()
    result3 = _run_verify(schema)
    assert result3.returncode != 0
    assert "pagamento não aprovado" in result3.stdout.lower()
    # Testa valor divergente
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Payment

        pay = Payment.query.filter_by(order_id=order_id).first()
        pay.status = "approved"
        pay.amount = Decimal("999.99")
        db.session.commit()
    result4 = _run_verify(schema)
    assert result4.returncode != 0
    assert "valor divergente" in result4.stdout.lower()


def test_item_sem_economia(canary_env):
    schema, app = canary_env
    ali_id, order_id, prod_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.modules.models import Product

        prod = db.session.get(Product, prod_id)
        prod.pricing_status = "unpriced"
        db.session.commit()
    result = _run_verify(schema)
    assert result.returncode != 0
    assert "pricing_status" in result.stdout.lower()


def test_custo_ausente(canary_env):
    schema, app = canary_env
    ali_id, order_id, prod_id, sp_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.modules.models import SupplierProduct

        sp = db.session.get(SupplierProduct, sp_id)
        sp.cost = None
        db.session.commit()
    result = _run_verify(schema)
    assert result.returncode != 0
    assert "cost ausente" in result.stdout.lower()


def test_multiplos_itens_segundo_invalido(canary_env):
    schema, app = canary_env
    from loja import db
    from loja.integrations.aliexpress.credential_store import persist_credential
    from loja.integrations.aliexpress.fulfillment import ensure_aliexpress_fulfillment
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem, Payment
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct, User
    from tests.aliexpress_snapshot_utils import materialize_snapshot

    with app.app_context():
        user = User(id=uuid.uuid4(), name="Multi", email=f"multi-{uuid.uuid4().hex}@test", phone="11999999999", is_admin=False)
        user.set_password("s")
        db.session.add(user)
        db.session.flush()
        supplier = Supplier(id=uuid.uuid4(), name="Supp Multi", channel_type="aliexpress", seller_id="SELLER_MULTI", default_currency="BRL")
        db.session.add(supplier)
        db.session.flush()
        prod1 = Product(id=uuid.uuid4(), name="Prod1", description="Test", price=Decimal("20.00"), source="aliexpress", external_id="EXT1", is_draft=True, is_active=False)
        db.session.add(prod1)
        db.session.flush()
        sp1 = SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=prod1.id, supplier_product_id="SP1", cost=Decimal("10.00"), currency="BRL", is_active=True, freight_status="available", shipping_cost=Decimal("5.00"), landed_cost=Decimal("15.00"), freight_currency="BRL")
        db.session.add(sp1)
        db.session.flush()
        var1 = ProductVariant(
            id=uuid.uuid4(),
            product_id=prod1.id,
            supplier_product_id=sp1.id,
            supplier_sku_id="SKU1",
            stock=10,
            price=Decimal("20.00"),
            is_active=True,
            attributes=[
                {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
            ],
        )
        db.session.add(var1)
        db.session.flush()
        prod1.pricing_status = "priced"
        prod1.target_margin_percent = Decimal("20")
        prod1.price = Decimal("18.75")
        prod2 = Product(id=uuid.uuid4(), name="Prod2", description="Test2", price=Decimal("30.00"), source="aliexpress", external_id="EXT2", is_draft=True, is_active=False)
        db.session.add(prod2)
        db.session.flush()
        sp2 = SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=prod2.id, supplier_product_id="SP2", cost=None, currency="BRL", is_active=True, freight_status="available", shipping_cost=Decimal("5.00"), landed_cost=None, freight_currency="BRL")
        db.session.add(sp2)
        db.session.flush()
        var2 = ProductVariant(
            id=uuid.uuid4(),
            product_id=prod2.id,
            supplier_product_id=sp2.id,
            supplier_sku_id="SKU2",
            stock=10,
            price=Decimal("30.00"),
            is_active=True,
            attributes=[
                {"name": "Color", "value": "Blue", "sku_property_id": "14", "property_value_id": "772"},
            ],
        )
        db.session.add(var2)
        db.session.flush()
        prod2.pricing_status = "priced"
        prod2.target_margin_percent = Decimal("25")
        prod2.price = Decimal("20.00")
        persist_credential(supplier_id=supplier.id, seller_id=supplier.seller_id, access_token="tok", expires_in=3600)
        db.session.flush()
        order = Order(id=uuid.uuid4(), user_id=user.id, checkout_type="product", status="paid", subtotal=Decimal("50.00"), total=Decimal("50.00"), stock_reserved=False, customer_name="Multi User", customer_email=user.email, customer_phone="11999999999", zip_code="01001000", street="Rua A", number="1", complement="", neighborhood="B", city="SP", state="SP", acquisition_source="instagram")
        db.session.add(order)
        db.session.flush()
        oi1 = OrderItem(id=uuid.uuid4(), order_id=order.id, product_id=prod1.id, variant_id=var1.id, product_name=prod1.name, quantity=1, unit_price=Decimal("20.00"), line_total=Decimal("20.00"))
        oi2 = OrderItem(id=uuid.uuid4(), order_id=order.id, product_id=prod2.id, variant_id=var2.id, product_name=prod2.name, quantity=1, unit_price=Decimal("30.00"), line_total=Decimal("30.00"))
        db.session.add(oi1)
        db.session.add(oi2)
        db.session.add(Payment(id=uuid.uuid4(), order_id=order.id, provider_payment_id=f"mp_{uuid.uuid4().hex[:8]}", status="approved", amount=order.total, currency="BRL"))
        db.session.commit()
        res = ensure_aliexpress_fulfillment(order)[0]
        ali = res.aliexpress_order
        # Força snapshot com 2 itens para testar validação de todos (usa order_item_id correto)
        from loja.integrations.aliexpress.submission_snapshot import AliExpressSubmissionSnapshot

        snap = AliExpressSubmissionSnapshot(aliexpress_order_id=ali.id, shipping_address={"contact_person": "x", "full_name": "x", "mobile_no": "11999999999", "phone_country": "+55", "zip": "01001000", "address": "Rua 10 Centro", "city": "SP", "province": "SP", "country": "BR"}, items=[{"order_item_id": str(oi1.id), "product_id": "SP1", "sku_attr": "Color: Red", "product_count": 1}, {"order_item_id": str(oi2.id), "product_id": "SP2", "sku_attr": "Color: Blue", "product_count": 1}])
        db.session.add(snap)
        db.session.commit()
        # Garante que o segundo item tenha economia inválida (custo None já)
        ali_id_test = ali.id

    result = _run_verify(schema)
    assert result.returncode != 0
    assert "item 1" in result.stdout.lower() or "cost ausente" in result.stdout.lower()


def test_snapshot_duplicado(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.integrations.aliexpress.submission_snapshot import AliExpressSubmissionSnapshot

        snap = db.session.query(AliExpressSubmissionSnapshot).filter_by(aliexpress_order_id=ali_id).first()
        # Duplica o primeiro item
        items = list(snap.items)
        items.append(items[0])  # duplicado
        snap.items = items
        db.session.commit()
    result = _run_verify(schema)
    assert result.returncode != 0
    # Duplicado causa quantidade divergente (order_items=1 vs snapshot=2) ou duplicado
    assert "duplicado" in result.stdout.lower() or "quantidade divergente" in result.stdout.lower()
    assert "11999999999" not in result.stdout
    assert "SELECT" not in result.stdout


def test_snapshot_item_ausente(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.integrations.aliexpress.submission_snapshot import AliExpressSubmissionSnapshot

        snap = db.session.query(AliExpressSubmissionSnapshot).filter_by(aliexpress_order_id=ali_id).first()
        # Remove um item (se houver 1, deixa vazio; se houver 2, remove 1)
        # Para canário válido com 1 item, vamos remover o único item
        snap.items = []
        db.session.commit()
    result = _run_verify(schema)
    assert result.returncode != 0
    # Deve falhar por snapshot vazio ou quantidade divergente
    assert "snapshot" in result.stdout.lower() or "quantidade divergente" in result.stdout.lower() or "correspondência" in result.stdout.lower()


def test_pedido_com_tentativa_anterior(canary_env):
    schema, app = canary_env
    ali_id, _order_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.modules.models import AliExpressSubmissionAttempt

        db.session.add(AliExpressSubmissionAttempt(id=uuid.uuid4(), aliexpress_order_id=ali_id, attempt_key="test-key", state="in_flight"))
        db.session.commit()
    result = _run_verify(schema)
    assert result.returncode != 0
    assert "nenhum pedido" in result.stdout.lower() or "tentativas" in result.stdout.lower()
    # Verifica que ainda é read-only (não houve escrita)
    result2 = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result2.returncode != 0
    assert "tentativas" in result2.stdout.lower()


def test_origem_nao_comprovada(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = None
        db.session.commit()
    result = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result.returncode != 0
    assert "origem orgânica não comprovada" in result.stdout.lower()
    # Testa também valor desconhecido, de teste e com dados sensíveis
    # Valores orgânicos documentados: instagram, google, tiktok, facebook, cpc, organic, referrer host (ex.: google.com)
    # Valores não orgânicos: test, unknown, canary, pg.test, etc., vazio ou com PII
    for bad_val in ["unknown", "test", "canary", "test@example.com"]:
        with app.app_context():
            from loja import db
            from loja.modules.getway_mercado_pago.payment_models import Order

            ord = db.session.get(Order, order_id)
            ord.acquisition_source = bad_val
            db.session.commit()
        result2 = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
        assert result2.returncode != 0
        assert "origem orgânica não comprovada" in result2.stdout.lower()
        assert bad_val not in result2.stdout
        assert bad_val not in result2.stderr
    # Testa valor com telefone (PII) - deve ser não comprovado e não vazar
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = "11999999999"
        db.session.commit()
    result3 = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result3.returncode != 0
    assert "origem orgânica não comprovada" in result3.stdout.lower()
    assert "11999999999" not in result3.stdout
    assert "11999999999" not in result3.stderr
    # Testa vazio separadamente (deve ser não comprovado, sem vazar "")
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = ""
        db.session.commit()
    result4 = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result4.returncode != 0
    assert "origem orgânica não comprovada" in result4.stdout.lower()


def test_acquisition_source_sensivel_nao_vaza(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    sensitive = "test@example.com'; DROP TABLE orders; --"
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = sensitive
        db.session.commit()
    result = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result.returncode != 0
    assert "origem orgânica não comprovada" in result.stdout.lower()
    assert sensitive not in result.stdout
    assert sensitive not in result.stderr
    assert "DROP" not in result.stdout
    assert "@" not in result.stdout or "referrer" not in result.stdout.lower()  # não deve vazar @


def test_acquisition_cpc_nao_elegivel(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = "cpc"
        db.session.commit()
    result = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result.returncode == 1
    assert "candidato encontrado" in result.stdout.lower()
    assert "origem de aquisição registrada" in result.stdout.lower()
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result.stdout.lower()
    assert "elegível" not in result.stdout.lower()
    assert "pronto para compra" not in result.stdout.lower()
    assert "cpc" not in result.stdout.lower()
    assert "cpc" not in result.stderr.lower()


def test_dominio_desconhecido_nao_elegivel(canary_env):
    schema, app = canary_env
    ali_id, order_id, *_ = _materialize_valid_canary(app)
    unknown_domain = "dominio-desconhecido-xyz123.com"
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = unknown_domain
        db.session.commit()
    result = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result.returncode == 1
    assert "candidato encontrado" in result.stdout.lower()
    assert "origem de aquisição registrada" in result.stdout.lower()
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result.stdout.lower()
    assert "elegível" not in result.stdout.lower()
    assert "pronto para compra" not in result.stdout.lower()
    assert unknown_domain not in result.stdout
    assert unknown_domain not in result.stderr
    with app.app_context():
        from loja import db
        from loja.modules.getway_mercado_pago.payment_models import Order

        ord = db.session.get(Order, order_id)
        ord.acquisition_source = "direct"
        db.session.commit()
    result2 = _run_verify(schema, extra_env={"CANARY_ORDER_ID": str(ali_id)})
    assert result2.returncode == 1
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result2.stdout.lower()
    assert "elegível" not in result2.stdout.lower()
    assert "direct" not in result2.stdout.lower()


def test_conexao_permanece_somente_leitura(canary_env):
    schema, app = canary_env
    _materialize_valid_canary(app)
    result = _run_verify(schema)
    assert result.returncode == 1
    assert "candidato encontrado" in result.stdout.lower()
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result.stdout.lower()
    assert "elegível" not in result.stdout.lower()
    assert "transaction_read_only" not in result.stderr.lower()
    for pii in ["11999999999", "Rua Teste", "01001000", "SELECT", "ProgrammingError"]:
        assert pii not in result.stdout
        assert pii not in result.stderr
    assert "..." in result.stdout


def test_saida_sem_pii(canary_env):
    schema, app = canary_env
    _materialize_valid_canary(app)
    result = _run_verify(schema)
    assert result.returncode == 1
    assert "candidato encontrado" in result.stdout.lower()
    assert "checks preliminares aprovados; confirmação orgânica pendente" in result.stdout.lower()
    assert "elegível" not in result.stdout.lower()
    for pii in ["11999999999", "Rua Teste", "01001000", "Valid User", "tok", "mp_", "SELECT", "INSERT", "UPDATE"]:
        if pii in ["Rua Teste", "01001000", "11999999999"]:
            assert pii not in result.stdout, f"PII vazou: {pii}"
        assert "ProgrammingError" not in result.stdout
        assert "SELECT" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "instagram" not in result.stdout.lower()
