from decimal import Decimal
from types import SimpleNamespace

import pytest

from loja.services.acquisition_costs import (
    AcquisitionCostUnavailable, acquisition_components, observed_amount,
)
from loja.services.pricing import calculate_landed_cost, sale_price_for_margin, PricingBlocked
from loja.services.order_unit_economics import resolve_order_landed_cost_snapshot


def test_observed_checkout_total_and_target_margin():
    link = SimpleNamespace(cost=Decimal('337.85'), shipping_cost=Decimal('0.00'),
        import_duty=Decimal('35.05'), icms=Decimal('76.42'),
        freight_status='available', currency='BRL', freight_currency='BRL')
    assert calculate_landed_cost(link) == Decimal('449.32')
    assert sale_price_for_margin(calculate_landed_cost(link), '30') == Decimal('641.89')


@pytest.mark.parametrize('value', [None, '', '-1', 'NaN', 'Infinity', True, '1.001', '100000000'])
def test_unverified_or_invalid_amount_is_not_zero(value):
    with pytest.raises(AcquisitionCostUnavailable):
        observed_amount(value)


def test_explicit_zero_taxes_and_free_shipping_are_valid():
    assert acquisition_components({'supplier_unit_cost': '10', 'import_duty': '0', 'icms': '0'}) == {
        'supplier_unit_cost': Decimal('10.00'), 'import_duty': Decimal('0.00'), 'icms': Decimal('0.00')}


@pytest.mark.parametrize('missing', ['import_duty', 'icms'])
def test_pricing_rejects_historical_unknown_tax(missing):
    values = dict(cost=Decimal('10'), shipping_cost=Decimal('0'), import_duty=Decimal('0'),
        icms=Decimal('0'), freight_status='available', currency='BRL', freight_currency='BRL')
    values[missing] = None
    with pytest.raises(PricingBlocked, match='supplier_taxes_unverified'):
        calculate_landed_cost(SimpleNamespace(**values))


def test_frozen_order_sums_taxes_once_and_legacy_stays_unknown():
    item = SimpleNamespace(quantity=1, supplier_unit_cost_snapshot='337.85',
        supplier_shipping_cost_snapshot='0', supplier_import_duty_snapshot='35.05',
        supplier_icms_snapshot='76.42', supplier_landed_cost_snapshot='449.32',
        supplier_cost_currency_snapshot='BRL')
    order = SimpleNamespace(items=[item])
    assert resolve_order_landed_cost_snapshot(order) == Decimal('449.32')
    item.supplier_icms_snapshot = None
    assert resolve_order_landed_cost_snapshot(order) is None


def test_test_suite_blocks_supplier_network_before_dns():
    import socket
    with pytest.raises(OSError, match='External network disabled'):
        socket.getaddrinfo('api-sg.aliexpress.com', 443)
