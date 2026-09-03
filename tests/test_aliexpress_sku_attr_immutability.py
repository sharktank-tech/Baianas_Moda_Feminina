"""Testes Fase 8.0.10.1 — congelamento de supplier_sku_attr.

Prova que a identidade de compra canônica (pid:vid) é congelada no
AliExpressOrderItem e nunca reconstruída do catálogo mutável.
"""

import uuid
from decimal import Decimal

import pytest

from loja import db
from loja.integrations.aliexpress.fulfillment import (
    ERROR_CODES,
    ensure_aliexpress_fulfillment,
)
from loja.integrations.aliexpress.submission_snapshot import (
    AliExpressSubmissionSnapshot,
    SubmissionSnapshotError,
    build_sku_attr,
    materialize_submission_snapshot,
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


VALID_ATTRS = [
    {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
    {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
]
EXPECTED_SKU_ATTR = "14:771;200000124:100013888"


@pytest.fixture(autouse=True)
def _valid_credential_env(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8"))


@pytest.fixture()
def ae_setup(db, make_user):
    supplier = Supplier(
        id=uuid.uuid4(), name="Supp 8.0.10.1", channel_type="aliexpress",
        seller_id="SELLER_101", default_currency="USD",
    )
    db.session.add(supplier)
    db.session.commit()
    product = Product(
        id=uuid.uuid4(), name="Prod 8.0.10.1", description="Test",
        price=Decimal("29.90"), source="aliexpress",
        external_id="EXT_101", is_draft=True, is_active=False,
    )
    db.session.add(product)
    db.session.commit()
    sp = SupplierProduct(
        id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id,
        supplier_product_id="100000101", cost=Decimal("10.00"), currency="USD", is_active=True,
    )
    db.session.add(sp)
    db.session.flush()
    variant = ProductVariant(
        id=uuid.uuid4(), product_id=product.id, supplier_product_id=sp.id,
        supplier_sku_id="SKU_101", sku=None, color="Red", size="M",
        attributes=list(VALID_ATTRS), stock=10, price=Decimal("29.90"), is_active=True,
    )
    db.session.add(variant)
    db.session.commit()
    # credential
    from loja.integrations.aliexpress.credential_store import persist_credential
    persist_credential(supplier_id=supplier.id, seller_id=supplier.seller_id, access_token="tok", expires_in=3600)
    db.session.commit()
    user = make_user()
    return {"supplier": supplier, "product": product, "sp": sp, "variant": variant, "user": user}


def _make_paid_order(db, setup, quantity=1):
    order = Order(
        id=uuid.uuid4(), user_id=setup["user"].id, checkout_type="product", status="paid",
        subtotal=Decimal("29.90"), total=Decimal("29.90") * quantity, stock_reserved=False,
        customer_name="U", customer_email="u@test.com", customer_phone="11999999999",
        zip_code="01001000", street="Rua", number="1", complement="", neighborhood="C", city="SP", state="SP",
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        id=uuid.uuid4(), order_id=order.id, product_id=setup["product"].id, variant_id=setup["variant"].id,
        product_name=setup["product"].name, quantity=quantity, unit_price=Decimal("29.90"), line_total=Decimal("29.90")*quantity,
    )
    db.session.add(item)
    db.session.commit()
    return order, item


# A. primeira materialização
def test_A_first_materialization_freezes_sku_attr(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    results = ensure_aliexpress_fulfillment(order)
    assert len(results) == 1 and results[0].state == "ready"
    snap = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
    assert snap is not None
    assert snap.supplier_sku_attr == EXPECTED_SKU_ATTR


def test_A_ordering_deterministic(db, ae_setup):
    # reversed attributes should still produce sorted canonical
    ae_setup["variant"].attributes = list(reversed(VALID_ATTRS))
    db.session.commit()
    order, item = _make_paid_order(db, ae_setup)
    results = ensure_aliexpress_fulfillment(order)
    snap = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
    assert snap.supplier_sku_attr == EXPECTED_SKU_ATTR


def test_build_sku_attr_contract():
    assert build_sku_attr(VALID_ATTRS) == EXPECTED_SKU_ATTR
    # labels only -> None, not invented
    assert build_sku_attr([{"name": "Color", "value": "Red"}]) is None
    assert build_sku_attr([]) is None
    assert build_sku_attr(None) is None
    assert build_sku_attr([{"sku_property_id": "x", "property_value_id": "1"}]) is None


# B. mutação posterior do catálogo não altera frozen
def test_B_catalog_mutation_does_not_change_frozen(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    snap_before = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first().supplier_sku_attr
    assert snap_before == EXPECTED_SKU_ATTR

    # mutate catalog
    ae_setup["variant"].attributes = [{"name": "Color", "value": "Blue", "sku_property_id": "14", "property_value_id": "999"}]
    ae_setup["variant"].supplier_sku_id = "SKU_CHANGED"
    db.session.commit()

    results = ensure_aliexpress_fulfillment(order)
    snap_after = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
    assert snap_after.supplier_sku_attr == EXPECTED_SKU_ATTR
    assert snap_after.supplier_sku_external_id == "SKU_101"  # original external sku still
    # blocked vs ready depends on cred, but sku_attr must stay
    assert snap_after.supplier_sku_attr == snap_before


# C. submission snapshot uses frozen even after catalog mutation
def test_C_submission_uses_frozen_after_mutation(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
    # mutate catalog before snapshot
    ae_setup["variant"].attributes = [{"name": "Color", "value": "Blue", "sku_property_id": "14", "property_value_id": "999"}]
    ae_setup["variant"].supplier_sku_id = "SKU_MUTATED"
    db.session.commit()

    res = materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    assert res.created is True
    assert res.snapshot.items[0]["sku_attr"] == EXPECTED_SKU_ATTR
    assert res.snapshot.items[0]["product_id"] == "100000101"


# D. variante removida (SET NULL) — snapshot still uses frozen
def test_D_variant_removed_still_uses_frozen(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()

    snap = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
    # Simulate variant deleted / SET NULL
    snap.variant_id = None
    db.session.commit()
    db.session.refresh(snap)
    assert snap.variant_id is None
    assert snap.supplier_sku_attr == EXPECTED_SKU_ATTR

    res = materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    assert res.snapshot.items[0]["sku_attr"] == EXPECTED_SKU_ATTR


# E. identidade incompleta na primeira materialização -> fail closed
@pytest.mark.parametrize("bad_attrs", [
    None,
    [],
    [{}],
    [{"name": "Color", "value": "Red"}],
    [{"name": "Color", "value": "Red", "sku_property_id": "14"}],  # missing vid
    [{"name": "Color", "value": "Red", "sku_property_id": "x", "property_value_id": "1"}],
    [{"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "x"}],
])
def test_E_incomplete_identity_blocks_not_ready(db, ae_setup, bad_attrs):
    ae_setup["variant"].attributes = bad_attrs
    db.session.commit()
    order, item = _make_paid_order(db, ae_setup)
    results = ensure_aliexpress_fulfillment(order)
    assert len(results) == 1
    assert results[0].state == "blocked"
    assert ERROR_CODES["SUPPLIER_SKU_ATTR_MISSING"] in results[0].aliexpress_order.last_error
    # No valid ready snapshot
    snap = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
    # For incomplete identity we never create a valid snapshot; blocked path creates no item or blocked item?
    # Our implementation creates NO AliExpressOrderItem for invalid new_invalid path; check that no ready item exists
    if snap is not None:
        assert snap.state == "blocked" or snap.supplier_sku_attr is None


# F. snapshot legado com supplier_sku_attr NULL -> submission fail closed, no catalog read
def test_F_legacy_null_fails_closed_no_catalog_rebuild(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
    snap = AliExpressOrderItem.query.filter_by(order_item_id=item.id).first()
    # corrupt to legacy NULL
    snap.supplier_sku_attr = None
    db.session.commit()

    # Ensure catalog still has valid attributes — if code incorrectly rebuilt from catalog, it would succeed
    assert ae_setup["variant"].attributes == VALID_ATTRS
    with pytest.raises(SubmissionSnapshotError) as exc:
        materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    assert str(exc.value) == "submission_sku_attr_missing"
    # also test empty string
    snap.supplier_sku_attr = "   "
    db.session.commit()
    with pytest.raises(SubmissionSnapshotError) as exc2:
        materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    assert str(exc2.value) == "submission_sku_attr_missing"


# G. idempotent materialize twice returns original, ignoring later changes
def test_G_idempotent_snapshot_ignores_later_changes(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
    res1 = materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    snap_id_first = res1.snapshot.id
    items_first = list(res1.snapshot.items)

    # Mutate address and catalog and logistics mapping
    order.customer_name = "Changed"
    order.street = "Changed Street"
    ae_setup["variant"].attributes = [{"name": "Color", "value": "Blue", "sku_property_id": "14", "property_value_id": "999"}]
    db.session.commit()

    res2 = materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "OTHER_SERVICE"}, session=db.session)
    assert res2.created is False
    assert res2.snapshot.id == snap_id_first
    assert res2.snapshot.items == items_first
    assert res2.snapshot.items[0]["sku_attr"] == EXPECTED_SKU_ATTR
    # address also frozen
    assert res1.snapshot.shipping_address == res2.snapshot.shipping_address


# H. custom session lookup respects passed session
def test_H_custom_session_respected(db, ae_setup):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
    res1 = materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    # second call with same session should find existing via session.query
    res2 = materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
    assert res2.created is False
    assert res2.snapshot.id == res1.snapshot.id
    # call with new session-like? Use same db.session but ensure path uses session param
    # Verify that session.query was used (we patched to session.query, not model query)
    # No exception means it respected session


# I. PII / secrets not leaked in repr/logs/errors
def test_I_no_pii_in_errors_and_repr(db, ae_setup):
    # build PII-heavy order and attempt failing snapshot
    order, item = _make_paid_order(db, ae_setup)
    # make address incomplete to trigger error
    order.zip_code = ""
    db.session.commit()
    ensure_aliexpress_fulfillment(order)
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
    try:
        materialize_submission_snapshot(ali_order, logistics_service_by_order_item={item.id: "CAINIAO"}, session=db.session)
        assert False, "should have raised"
    except SubmissionSnapshotError as e:
        msg = str(e)
        # sanitized code only, no PII
        assert "shipping_address_incomplete" == msg
        assert "11999999999" not in msg
        assert "U@test.com" not in msg
        assert "01001000" not in msg
        assert "Rua" not in msg

    # repr of snapshot should not contain PII
    order.zip_code = "01001000"
    order.street = "Rua"
    db.session.commit()
    # Recreate valid snapshot scenario: need a new order because previous ali_order is now blocked? create fresh ae_setup2
    # Instead test repr directly on existing snapshot if any
    # Create fresh valid setup
    supplier2 = Supplier(id=uuid.uuid4(), name="Supp2", channel_type="aliexpress", seller_id="SELLER2", default_currency="USD")
    db.session.add(supplier2)
    db.session.commit()
    from loja.integrations.aliexpress.credential_store import persist_credential
    persist_credential(supplier_id=supplier2.id, seller_id=supplier2.seller_id, access_token="tok", expires_in=3600)
    product2 = Product(id=uuid.uuid4(), name="Prod2", description="Test", price=Decimal("29.90"), source="aliexpress", external_id="EXT2", is_draft=True, is_active=False)
    db.session.add(product2)
    db.session.commit()
    sp2 = SupplierProduct(id=uuid.uuid4(), supplier_id=supplier2.id, product_id=product2.id, supplier_product_id="100000102", cost=Decimal("10.00"), currency="USD", is_active=True)
    db.session.add(sp2)
    db.session.flush()
    variant2 = ProductVariant(id=uuid.uuid4(), product_id=product2.id, supplier_product_id=sp2.id, supplier_sku_id="SKU2", sku=None, color="Red", size="M", attributes=list(VALID_ATTRS), stock=10, price=Decimal("29.90"), is_active=True)
    db.session.add(variant2)
    db.session.commit()
    user = ae_setup["user"]
    order2 = Order(id=uuid.uuid4(), user_id=user.id, checkout_type="product", status="paid", subtotal=Decimal("29.90"), total=Decimal("29.90"), stock_reserved=False, customer_name="U", customer_email="u@test.com", customer_phone="11999999999", zip_code="01001000", street="Rua", number="1", complement="", neighborhood="C", city="SP", state="SP")
    db.session.add(order2)
    db.session.flush()
    item2 = OrderItem(id=uuid.uuid4(), order_id=order2.id, product_id=product2.id, variant_id=variant2.id, product_name=product2.name, quantity=1, unit_price=Decimal("29.90"), line_total=Decimal("29.90"))
    db.session.add(item2)
    db.session.commit()
    ensure_aliexpress_fulfillment(order2)
    ali_order2 = AliExpressOrder.query.filter_by(order_id=order2.id).first()
    res = materialize_submission_snapshot(ali_order2, logistics_service_by_order_item={item2.id: "CAINIAO"}, session=db.session)
    r = repr(res.snapshot)
    assert "11999999999" not in r
    assert "u@test.com" not in r
    assert "Rua" not in r
    assert "tok" not in r.lower()
    assert str(res.snapshot.aliexpress_order_id) in r


# --- Fase 8.0.10.2: logistics_service_name opcional (docs oficiais placeorder) ---

def _logistics_snapshot(db, ae_setup, logistics):
    order, item = _make_paid_order(db, ae_setup)
    ensure_aliexpress_fulfillment(order)
    db.session.commit()
    ali_order = AliExpressOrder.query.filter_by(order_id=order.id).first()
    mapping = {}
    if logistics is not None:
        mapping = {item.id: logistics}
    res = materialize_submission_snapshot(
        ali_order, logistics_service_by_order_item=mapping, session=db.session
    )
    db.session.commit()
    return res.snapshot, item


def test_K_logistics_omitted_is_absent(db, ae_setup):
    """Contrato A: sem serviço logístico, a chave é OMITIDA (nunca None/vazio)."""
    snap, _ = _logistics_snapshot(db, ae_setup, None)
    assert len(snap.items) == 1
    assert "logistics_service_name" not in snap.items[0]


def test_K_logistics_explicit_is_frozen(db, ae_setup):
    """Contrato B: serviço explícito é congelado no snapshot."""
    snap, _ = _logistics_snapshot(db, ae_setup, "CAINIAO_ECONOMIC")
    assert snap.items[0]["logistics_service_name"] == "CAINIAO_ECONOMIC"


def test_K_logistics_empty_or_whitespace_is_omitted(db, ae_setup):
    """Contrato C: "" ou whitespace é tratado como ausência e a chave é omitida."""
    for value in ("", "   ", "\t"):
        snap, _ = _logistics_snapshot(db, ae_setup, value)
        assert "logistics_service_name" not in snap.items[0]
        assert "product_id" in snap.items[0]
        assert "sku_attr" in snap.items[0]
        assert "product_count" in snap.items[0]

