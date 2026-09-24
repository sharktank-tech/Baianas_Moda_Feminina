"""FASE 8.2.0 — dashboard administrativo read-only de unit economics.

Cobre o serviço agregador e a rota GET /admin/unit-economics (somente leitura).
"""
import uuid
from decimal import Decimal

import pytest

from loja.modules.models import Product, SupplierProduct, ProductVariant
from loja.services.publication_unit_economics_dashboard import (
    build_unit_economics_dashboard,
    dashboard_totals,
)
from loja.services.unit_economics_policy import UnitEconomicsCostPolicy


def _prepared_product(db, *, price="100.00", landed="70.00", n_links=1):
    product = Product(
        id=uuid.uuid4(),
        name="Camiseta Dashboard",
        description="teste",
        price=Decimal(price),
        is_active=True,
    )
    db.session.add(product)
    db.session.flush()
    for _ in range(n_links):
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id="SP_" + uuid.uuid4().hex[:8],
            cost=Decimal("60.00"),
            currency="BRL",
            freight_status="available",
            shipping_cost=Decimal("10.00"),
            landed_cost=Decimal(landed) if landed is not None else None,
            freight_currency="BRL",
            is_active=True,
        )
        db.session.add(sp)
        db.session.flush()
    variant = ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id=sp.id,
        stock=5,
        is_active=True,
    )
    db.session.add(variant)
    db.session.commit()
    return product


def _complete_policy():
    return UnitEconomicsCostPolicy(
        gateway_fee_percent=Decimal("3.00"),
        installment_cost_percent=Decimal("1.00"),
        tax_percent=Decimal("2.00"),
        acquisition_cost_per_order=Decimal("1.00"),
        refund_reserve_percent=Decimal("1.00"),
    )


# ---------------------------------------------------------------------------
# Serviço read-only
# ---------------------------------------------------------------------------

def test_dashboard_row_compute_margins(db):
    product = _prepared_product(db, price="100.00", landed="70.00")
    rows = build_unit_economics_dashboard([product], policy=_complete_policy())

    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ready"
    assert row.complete is True
    assert row.sale_price == Decimal("100.00")
    assert row.landed_cost == Decimal("70.00")
    # à vista: gateway 3% + desconto 0 + imposto 2% = 5.00
    assert row.cash_margin == Decimal("23.00")
    assert row.cash_margin_percent == Decimal("23.00")
    # parcelada: parcelamento 1% + desconto 0 + imposto 2% = 3.00
    assert row.installment_margin == Decimal("25.00")
    assert row.installment_margin_percent == Decimal("25.00")
    # pior cenário (à vista) alimenta a margem consolidada
    assert row.known_variable_costs == Decimal("5.00")
    assert row.known_operational_costs == Decimal("2.00")
    assert row.contribution_margin == Decimal("25.00")
    assert row.contribution_margin_percent == Decimal("25.00")
    assert row.operational_margin == Decimal("23.00")
    assert row.operational_margin_percent == Decimal("23.00")
    assert row.unknown_components == ()


def test_dashboard_incomplete_when_policy_unknown(db):
    product = _prepared_product(db, price="100.00", landed="70.00")
    policy = UnitEconomicsCostPolicy(
        gateway_fee_percent=None,
        installment_cost_percent=None,
        tax_percent=None,
        acquisition_cost_per_order=None,
        refund_reserve_percent=None,
    )
    rows = build_unit_economics_dashboard([product], policy=policy)

    assert len(rows) == 1
    row = rows[0]
    assert row.complete is False
    assert row.status == "unit_economics_incomplete"
    assert row.contribution_margin is None
    assert row.operational_margin is None
    assert row.known_variable_costs is None
    assert row.sale_price == Decimal("100.00")
    assert row.landed_cost == Decimal("70.00")
    assert "payment_fee" in row.unknown_components
    assert "tax" in row.unknown_components


def test_dashboard_supplier_missing(db):
    product = _prepared_product(db)
    product.supplier_links = []

    rows = build_unit_economics_dashboard([product], policy=_complete_policy())

    assert len(rows) == 1
    assert rows[0].status == "supplier_missing"
    assert rows[0].complete is False
    assert rows[0].landed_cost is None


def test_dashboard_supplier_ambiguous(db):
    product = _prepared_product(db, n_links=2)

    rows = build_unit_economics_dashboard([product], policy=_complete_policy())

    assert len(rows) == 1
    assert rows[0].status == "supplier_ambiguous"
    assert rows[0].complete is False
    assert rows[0].landed_cost is None


def test_dashboard_landed_missing(db):
    product = _prepared_product(db, landed=None)

    rows = build_unit_economics_dashboard([product], policy=_complete_policy())

    assert len(rows) == 1
    assert rows[0].status == "landed_cost_missing_or_invalid"
    assert rows[0].complete is False
    assert rows[0].sale_price == Decimal("100.00")
    assert rows[0].landed_cost is None


def test_dashboard_totals(db):
    first = _prepared_product(db, price="100.00", landed="70.00")
    second = _prepared_product(db, price="50.00", landed="20.00")

    products = [first, second]
    rows = build_unit_economics_dashboard(products, policy=_complete_policy())
    totals = dashboard_totals(rows)

    assert totals["total_products"] == 2
    assert totals["complete"] == 2
    assert totals["incomplete"] == 0
    assert totals["total_revenue"] == Decimal("150.00")
    assert totals["total_costs"] == Decimal("90.00")


# ---------------------------------------------------------------------------
# Rota admin (somente leitura)
# ---------------------------------------------------------------------------

def test_unit_economics_route_admin(client, admin_user, login, db):
    _prepared_product(db, price="100.00", landed="70.00")
    login(admin_user.email, "senha123")

    response = client.get("/admin/unit-economics")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Unit Economics" in body
    assert "Camiseta Dashboard" in body
    assert "R$ 100.00" in body
    assert "R$ 70.00" in body


def test_unit_economics_route_anonymous_redirect(client):
    response = client.get("/admin/unit-economics")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_unit_economics_route_non_admin_denied(client, regular_user, login):
    login(regular_user.email, "senha123")
    response = client.get("/admin/unit-economics", follow_redirects=True)
    assert response.status_code == 200
    assert "Unit Economics" not in response.get_data(as_text=True)