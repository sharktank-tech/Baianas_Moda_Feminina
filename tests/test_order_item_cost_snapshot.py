from decimal import Decimal
from types import SimpleNamespace

from loja.views.payments_routes import _supplier_cost_snapshot


EMPTY = {
    "supplier_unit_cost_snapshot": None,
    "supplier_shipping_cost_snapshot": None,
    "supplier_landed_cost_snapshot": None,
    "supplier_cost_currency_snapshot": None,
}


def _product(*links):
    return SimpleNamespace(supplier_links=list(links))


def _link(**overrides):
    values = {
        "is_active": True,
        "cost": Decimal("30.00"),
        "shipping_cost": Decimal("7.50"),
        "landed_cost": Decimal("37.50"),
        "currency": "BRL",
        "freight_currency": "BRL",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_supplier_cost_snapshot_freezes_complete_canonical_values():
    assert _supplier_cost_snapshot(_product(_link())) == {
        "supplier_unit_cost_snapshot": Decimal("30.00"),
        "supplier_shipping_cost_snapshot": Decimal("7.50"),
        "supplier_landed_cost_snapshot": Decimal("37.50"),
        "supplier_cost_currency_snapshot": "BRL",
    }


def test_supplier_cost_snapshot_keeps_missing_cost_unknown():
    assert _supplier_cost_snapshot(
        _product(_link(landed_cost=None))
    ) == EMPTY


def test_supplier_cost_snapshot_rejects_incoherent_landed_cost():
    assert _supplier_cost_snapshot(
        _product(_link(landed_cost=Decimal("99.00")))
    ) == EMPTY


def test_supplier_cost_snapshot_rejects_currency_mismatch():
    assert _supplier_cost_snapshot(
        _product(_link(freight_currency="USD"))
    ) == EMPTY


def test_supplier_cost_snapshot_requires_exactly_one_active_supplier():
    assert _supplier_cost_snapshot(_product()) == EMPTY
    assert _supplier_cost_snapshot(_product(_link(), _link())) == EMPTY
