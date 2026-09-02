"""Testes da preparação canônica do placeorder (Fase 8.0.10).

Não fazem HTTP e não executam compra real.
"""

import pytest

from loja.integrations.aliexpress.submission_snapshot import (
    SubmissionSnapshotError,
    build_sku_attr,
)


def test_build_sku_attr_canonicaliza_por_property_id():
    attrs = [
        {
            "name": "Size",
            "value": "M",
            "sku_property_id": "200000124",
            "property_value_id": "100013888",
        },
        {
            "name": "Color",
            "value": "Black",
            "sku_property_id": "14",
            "property_value_id": "771",
        },
    ]
    assert build_sku_attr(attrs) == "14:771;200000124:100013888"


def test_build_sku_attr_nao_inventa_ids_por_label():
    attrs = [
        {"name": "Color", "value": "Black"},
        {"name": "Size", "value": "M"},
    ]
    assert build_sku_attr(attrs) is None


@pytest.mark.parametrize(
    "attrs",
    [
        None,
        [],
        [{}],
        [{"sku_property_id": "x", "property_value_id": "1"}],
        [{"sku_property_id": "1", "property_value_id": "x"}],
        [{"sku_property_id": "1"}],
    ],
)
def test_build_sku_attr_fail_closed(attrs):
    assert build_sku_attr(attrs) is None


def test_submission_snapshot_error_nao_carrega_pii():
    exc = SubmissionSnapshotError("shipping_address_incomplete")
    assert str(exc) == "shipping_address_incomplete"
    assert "address" in str(exc)
    assert "@" not in str(exc)
