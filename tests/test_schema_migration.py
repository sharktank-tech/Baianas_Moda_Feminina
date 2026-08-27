"""Testes de migração Fase 0 (bootstrap de schema aditivo/idempotente).

Cobertura exigida:
- create_app() sobe sem erro
- db.create_all() cria as novas tabelas
- Product.category (String legado), Product.category_obj, Product.category_id
- CartItem.variant_id, OrderItem.variant_id
- Category.parent/children
- SupplierProduct unique, ProductVariant unique
- ensure_schema() pode rodar 2x (idempotente)
- ensure_schema() num banco "antigo" simulado (coluna/FK/índice ausentes)
"""
import os
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine, text, inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from loja import db, create_app
from loja.schema import ensure_schema
from loja.modules.models import (
    Product,
    Category,
    ProductVariant,
    SupplierProduct,
    CartItem,
    Cart,
    OrderItem,
    Supplier,
)


def test_create_app_boots_without_error():
    app = create_app()
    assert app is not None


def test_new_tables_created_by_create_all(db):
    expected = {
        "categories",
        "product_images",
        "product_variants",
        "supplier_products",
        "supplier_category_mappings",
        "product_syncs",
        "aliexpress_orders",
        "sync_logs",
    }
    inspector = sa_inspect(db.engine)
    assert expected.issubset(set(inspector.get_table_names()))


def test_product_category_string_still_works(db):
    p = Product(
        id=uuid.uuid4(), name="Camiseta", price=10, image1="x",
        category="masculino", stock=5, is_active=True,
    )
    db.session.add(p)
    db.session.commit()
    assert db.session.get(Product, p.id).category == "masculino"


def test_product_category_obj_relationship(db):
    cat = Category(name="Vestuário", slug="vestuario")
    db.session.add(cat)
    db.session.commit()
    p = Product(
        id=uuid.uuid4(), name="Calça", price=20, image1="x",
        category="feminino", stock=3, is_active=True, category_obj=cat,
    )
    db.session.add(p)
    db.session.commit()
    fetched = db.session.get(Product, p.id)
    assert fetched.category_obj.id == cat.id
    assert cat.products  # back_populates coerente via category_obj


def test_product_category_id_works(db):
    cat = Category(name="Infantil", slug="infantil")
    db.session.add(cat)
    db.session.commit()
    p = Product(
        id=uuid.uuid4(), name="Bermuda", price=30, image1="x",
        category="infantil", stock=2, is_active=True, category_id=cat.id,
    )
    db.session.add(p)
    db.session.commit()
    assert db.session.get(Product, p.id).category_id == cat.id


def test_cart_item_variant_id(db):
    p = Product(
        id=uuid.uuid4(), name="P", price=10, image1="x",
        category="c", stock=5, is_active=True,
    )
    v = ProductVariant(product_id=p.id, sku="S", stock=1)
    db.session.add_all([p, v])
    db.session.commit()
    cart = Cart(id=uuid.uuid4(), user_id=uuid.uuid4())
    ci = CartItem(
        cart_id=cart.id, product_id=p.id, selected_size="M",
        quantity=1, unit_price=10, variant_id=v.id,
    )
    db.session.add_all([cart, ci])
    db.session.commit()
    assert db.session.get(CartItem, ci.id).variant_id == v.id


def test_order_item_variant_id(db):
    p = Product(
        id=uuid.uuid4(), name="P", price=10, image1="x",
        category="c", stock=5, is_active=True,
    )
    v = ProductVariant(product_id=p.id, sku="S", stock=1)
    db.session.add_all([p, v])
    db.session.commit()
    oi = OrderItem(
        order_id=uuid.uuid4(), product_id=p.id, product_name="P",
        selected_size="M", variant_id=v.id, quantity=1,
        unit_price=10, line_total=10,
    )
    db.session.add(oi)
    db.session.commit()
    assert db.session.get(OrderItem, oi.id).variant_id == v.id


def test_category_parent_children(db):
    parent = Category(name="Pai", slug="pai")
    child = Category(name="Filho", slug="filho", parent=parent)
    db.session.add_all([parent, child])
    db.session.commit()
    fetched = db.session.get(Category, child.id)
    assert fetched.parent.id == parent.id
    assert child in parent.children


def test_supplier_product_unique(db):
    s = Supplier(name="Fornecedor", channel_type="manual")
    db.session.add(s)
    db.session.commit()
    prod = Product(
        id=uuid.uuid4(), name="P", price=10, image1="x",
        category="c", stock=5, is_active=True,
    )
    db.session.add(prod)
    db.session.commit()
    sp1 = SupplierProduct(
        supplier_id=s.id, product_id=prod.id, supplier_product_id="EXT1",
    )
    sp2 = SupplierProduct(
        supplier_id=s.id, product_id=prod.id, supplier_product_id="EXT1",
    )
    db.session.add_all([sp1, sp2])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_product_variant_unique(db):
    prod = Product(
        id=uuid.uuid4(), name="P", price=10, image1="x",
        category="c", stock=5, is_active=True,
    )
    db.session.add(prod)
    db.session.commit()
    pv1 = ProductVariant(
        product_id=prod.id, supplier_product_id=uuid.uuid4(),
        supplier_sku_id="SKU1", stock=0,
    )
    pv2 = ProductVariant(
        product_id=prod.id, supplier_product_id=pv1.supplier_product_id,
        supplier_sku_id="SKU1", stock=0,
    )
    db.session.add_all([pv1, pv2])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_ensure_schema_idempotent(app, db):
    ensure_schema(app)
    cols = {c["name"] for c in sa_inspect(db.engine).get_columns("products")}
    assert {"category_id", "external_id", "source", "cost"}.issubset(cols)
    # segunda execução não deve quebrar nem duplicar
    ensure_schema(app)
    cols2 = {c["name"] for c in sa_inspect(db.engine).get_columns("products")}
    assert cols == cols2


def test_ensure_schema_on_simulated_existing_db():
    """Banco 'antigo': tabelas existentes sem colunas/FKs novas; tabelas
    novas AUSENTES. Simula produção antes do deploy e valida o bootstrap
    aditivo + idempotência.
    """
    from flask import Flask

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    uri = "sqlite:///%s" % tmp.name

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SECRET_KEY"] = "test-secret-key"
    app.config["TESTING"] = True
    db.init_app(app)

    # schema antigo manual (sem colunas novas/FKs)
    pre = create_engine(uri)
    with pre.connect() as c:
        c.execute(text(
            "CREATE TABLE orders (id VARCHAR(36) PRIMARY KEY, order_number TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE suppliers (id VARCHAR(36) PRIMARY KEY, name TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE products (id VARCHAR(36) PRIMARY KEY, name TEXT, "
            "price TEXT, stock INTEGER, category TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE cart_items (id VARCHAR(36) PRIMARY KEY, "
            "cart_id VARCHAR(36), product_id VARCHAR(36), selected_size TEXT, "
            "quantity INTEGER, unit_price TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE order_items (id VARCHAR(36) PRIMARY KEY, "
            "order_id VARCHAR(36), product_id VARCHAR(36), product_name TEXT, "
            "quantity INTEGER, unit_price TEXT, line_total TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE shipments (id INTEGER PRIMARY KEY, "
            "order_id VARCHAR(36), tracking_code TEXT)"
        ))
        c.commit()

    try:
        with app.app_context():
            db.create_all()  # cria apenas tabelas NOVAS
            ensure_schema(app)

            inspector = sa_inspect(db.engine)

            prod_cols = {c["name"] for c in inspector.get_columns("products")}
            assert "category_id" in prod_cols
            assert "external_id" in prod_cols
            assert "source" in prod_cols
            assert "cost" in prod_cols

            fks = inspector.get_foreign_keys("products")
            assert any(
                f.get("referred_table") == "categories"
                and "category_id" in (f.get("constrained_columns") or [])
                for f in fks
            )

            idxs = inspector.get_indexes("products")
            assert any(
                "category_id" in (ix.get("column_names") or []) for ix in idxs
            )

            ci_cols = {c["name"] for c in inspector.get_columns("cart_items")}
            assert "variant_id" in ci_cols
            ci_fks = inspector.get_foreign_keys("cart_items")
            assert any(
                f.get("referred_table") == "product_variants"
                and "variant_id" in (f.get("constrained_columns") or [])
                for f in ci_fks
            )

            sh_cols = {c["name"] for c in inspector.get_columns("shipments")}
            assert "ali_order_id" in sh_cols
            assert "external_shipment_id" in sh_cols

            # idempotência: rodar de novo não deve quebrar nem duplicar
            ensure_schema(app)
            prod_cols2 = {
                c["name"] for c in sa_inspect(db.engine).get_columns("products")
            }
            assert prod_cols == prod_cols2
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
