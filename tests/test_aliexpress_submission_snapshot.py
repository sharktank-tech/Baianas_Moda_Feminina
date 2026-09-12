"""Testes da preparação canônica do placeorder.

Não fazem HTTP e não executam compra real.
"""

from types import SimpleNamespace

import pytest

from loja.integrations.aliexpress.submission_snapshot import (
    SubmissionSnapshotError,
    _normalize_br_mobile,
    _shipping_address,
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11987654321", "11987654321"),
        ("(11) 98765-4321", "11987654321"),
        ("+55 (11) 98765-4321", "11987654321"),
    ],
)
def test_normalize_br_mobile_accepts_national_or_plus55(raw, expected):
    assert _normalize_br_mobile(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "55123456789",
        "+55 11 8765-4321",
        "0011987654321",
    ],
)
def test_normalize_br_mobile_rejects_malformed_without_inventing_digits(raw):
    with pytest.raises(SubmissionSnapshotError) as exc:
        _normalize_br_mobile(raw)
    assert exc.value.args[0] == "shipping_mobile_invalid"


def _order(**overrides):
    data = {
        "customer_name": "Cliente Teste",
        "customer_phone": "+55 (11) 98765-4321",
        "zip_code": "01001000",
        "street": "Rua Teste",
        "number": "123",
        "complement": "Apto 10",
        "neighborhood": "Centro",
        "city": "São Paulo",
        "state": "sp",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_shipping_address_separates_brazil_fields():
    shipping = _shipping_address(_order())
    assert shipping == {
        "contact_person": "Cliente Teste",
        "full_name": "Cliente Teste",
        "mobile_no": "11987654321",
        "phone_country": "+55",
        "zip": "01001000",
        "address": "Rua Teste 123 Centro",
        "address2": "Apto 10",
        "city": "São Paulo",
        "province": "SP",
        "country": "BR",
    }


def test_shipping_address_omits_empty_address2():
    shipping = _shipping_address(_order(complement=""))
    assert "address2" not in shipping
    assert shipping["address"] == "Rua Teste 123 Centro"


def test_shipping_address_rejects_invalid_mobile_before_snapshot():
    with pytest.raises(SubmissionSnapshotError) as exc:
        _shipping_address(_order(customer_phone="55123456789"))
    assert exc.value.args[0] == "shipping_mobile_invalid"
