"""Testes offline de aliexpress.logistics.buyer.freight.get (Fase 4b isolada)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import (
    AliExpressClient,
    build_buyer_freight_get_params,
    canonical_freight_sku_id,
    freight_product_id,
    freight_product_sku_shape,
    normalize_buyer_freight_get_response,
    raw_canonical_sku_id,
)
from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.models import AliExpressProductDetail, AliExpressProductSku
from loja.integrations.aliexpress.sign import sign_sync


def _mksku(
    sku_id: str = "s1", stock: int = 5, properties=None, **raw_extra
) -> AliExpressProductSku:
    raw = dict(raw_extra)
    if properties is not None:
        raw["aeop_s_k_u_propertys"] = properties
    return AliExpressProductSku(
        sku_id=sku_id,
        available_stock=stock,
        attributes=[],
        raw=raw,
    )


def _mkdetail(product_id="100500123", raw=None) -> AliExpressProductDetail:
    return AliExpressProductDetail(
        product_id=product_id,
        title="p",
        images=["https://x/a.jpg"],
        skus=[],
        raw=raw or {},
    )


class _FakeTransport:
    def __init__(self, body: str = "{}"):
        self.body = body
        self.last_params: dict = {}

    def post(self, params: dict) -> str:
        self.last_params = dict(params)
        return self.body


# ----- parâmetros obrigatórios ---------------------------------------------
def test_get_requires_product_id():
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id="", sku_id="14:771")


def test_get_requires_sku_id():
    # sku_id é obrigatório e vai no DTO do freight.get.
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id=123, sku_id="")
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id=123, sku_id=None)


def test_get_requires_country_and_origin():
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id=123, sku_id="s1", country_code="")
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id=123, sku_id="s1", send_goods_country_code="")


def test_get_product_id_non_numeric_raises():
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id="ABC", sku_id="s1")


def test_get_product_num_minimum():
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id=123, sku_id="s1", product_num=0)


# ----- tipos JSON e omitidos quando vazios ----------------------------------
def test_get_dto_json_types_with_sku():
    p = build_buyer_freight_get_params(product_id=123, sku_id="14:771")
    dto = json.loads(p["aeopFreightCalculateForBuyerDTO"])
    assert dto["product_id"] == 123
    assert isinstance(dto["product_id"], int)
    assert dto["product_num"] == 1
    assert isinstance(dto["product_num"], int)
    assert dto["sku_id"] == "14:771"
    assert dto["country_code"] == "BR"
    assert dto["send_goods_country_code"] == "CN"


def test_get_product_id_string_becomes_number():
    p = build_buyer_freight_get_params(product_id="123", sku_id="14:771")
    dto = p["aeopFreightCalculateForBuyerDTO"]
    assert '"product_id":123' in dto


def test_get_sku_id_in_json_snake_case():
    p = build_buyer_freight_get_params(product_id=123, sku_id="14:771;5:100014064")
    dto = p["aeopFreightCalculateForBuyerDTO"]
    assert '"sku_id":"14:771;5:100014064"' in dto


def test_get_province_city_omitted_when_empty():
    p = build_buyer_freight_get_params(product_id=123, sku_id="14:771")
    assert "province_code" not in p["aeopFreightCalculateForBuyerDTO"]
    assert "city_code" not in p["aeopFreightCalculateForBuyerDTO"]


def test_get_province_city_included_when_set():
    p = build_buyer_freight_get_params(
        product_id=123, sku_id="14:771", province_code="SP", city_code="SAO_PAULO"
    )
    dto = p["aeopFreightCalculateForBuyerDTO"]
    assert '"province_code":"SP"' in dto
    assert '"city_code":"SAO_PAULO"' in dto


# ----- SKU canônico AliExpress ----------------------------------------------
def test_canonical_single_property():
    sku = _mksku(properties=[{"sku_property_id": "14", "property_value_id": "771"}])
    assert canonical_freight_sku_id(sku) == "14:771"


def test_canonical_two_properties():
    sku = _mksku(
        properties=[
            {"sku_property_id": "14", "property_value_id": "771"},
            {"sku_property_id": "5", "property_value_id": "100014064"},
        ]
    )
    assert canonical_freight_sku_id(sku) == "14:771;5:100014064"


def test_canonical_property_value_id_long_preferred():
    sku = _mksku(
        properties=[
            {
                "sku_property_id": "14",
                "property_value_id": "777",
                "property_value_id_long": "771",
            }
        ]
    )
    assert canonical_freight_sku_id(sku) == "14:771"


def test_canonical_property_value_id_fallback():
    sku = _mksku(properties=[{"sku_property_id": "14", "property_value_id": "771"}])
    assert canonical_freight_sku_id(sku) == "14:771"


def test_canonical_wrapper_ae_sku_property_dtos():
    sku = _mksku()
    sku.raw["ae_sku_property_dtos"] = {
        "ae_sku_property_d_t_o": [
            {"sku_property_id": "5", "property_value_id": "9"},
            {"sku_property_id": "6", "property_value_id": "10"},
        ]
    }
    assert canonical_freight_sku_id(sku) == "5:9;6:10"


def test_canonical_raw_id_when_no_properties():
    sku = _mksku(sku_id="200000182:193;200007763:201336100")
    assert canonical_freight_sku_id(sku) == "200000182:193;200007763:201336100"


def test_canonical_raw_sku_id_priority_over_derived():
    # O sku.id bruto canônico NÃO é substituído pelas propriedades.
    sku = _mksku(
        sku_id="14:99",
        properties=[{"sku_property_id": "5", "property_value_id": "9"}],
    )
    assert canonical_freight_sku_id(sku) == "14:99"
    assert raw_canonical_sku_id(sku) == "14:99"


def test_raw_canonical_sku_id_invalid_returns_none():
    assert raw_canonical_sku_id(_mksku(sku_id="s1")) is None
    assert raw_canonical_sku_id(_mksku(sku_id="14:771:extra")) is None
    assert raw_canonical_sku_id(_mksku(sku_id="")) is None
    assert raw_canonical_sku_id(None) is None


def test_canonical_raw_id_trimmed_and_unquoted():
    sku = _mksku(sku_id='  "14:771"  ')
    assert canonical_freight_sku_id(sku) == "14:771"


def test_canonical_raw_id_invalid_rejected():
    assert canonical_freight_sku_id(_mksku(sku_id="s1")) is None
    assert canonical_freight_sku_id(_mksku(sku_id="14:771:extra")) is None
    assert canonical_freight_sku_id(_mksku(sku_id="abc:def")) is None


def test_canonical_never_uses_sku_code_or_barcode():
    sku = _mksku(sku_id="s1", sku_code="987654321", barcode="999")
    assert canonical_freight_sku_id(sku) is None


def test_canonical_no_names_after_hash():
    sku = _mksku(
        properties=[
            {
                "sku_property_id": "14",
                "property_value_id": "771",
                "sku_property_name": "Cor",
                "property_value_definition_name": "Azul",
            }
        ]
    )
    assert canonical_freight_sku_id(sku) == "14:771"
    assert "#" not in canonical_freight_sku_id(sku)


def test_canonical_none_and_empty_skus():
    assert canonical_freight_sku_id(None) is None
    assert canonical_freight_sku_id(_mksku(sku_id="")) is None


def test_freight_product_id_uses_main_product_id():
    detail = _mkdetail(
        raw={
            "product_id_converter_result": {
                "main_product_id": "2000000001",
                "sub_product_id": "100500123",
            }
        }
    )
    assert freight_product_id(detail) == "2000000001"


def test_freight_product_id_falls_back_to_base_when_absent():
    assert freight_product_id(_mkdetail()) == "100500123"
    detail = _mkdetail(raw={"product_id_converter_result": {}})
    assert freight_product_id(detail) == "100500123"
    empty = _mkdetail(raw={"product_id_converter_result": {"main_product_id": ""}})
    assert freight_product_id(empty) == "100500123"


def test_freight_product_id_invalid_converter_falls_back_safely():
    detail = _mkdetail(raw={"product_id_converter_result": "oops"})
    assert freight_product_id(detail) == "100500123"
    none_raw = _mkdetail(raw=None)
    assert freight_product_id(none_raw) == "100500123"


def test_freight_product_sku_shape_sensitive_fields_only():
    detail = _mkdetail(
        raw={
            "product_id_converter_result": {
                "main_product_id": "2000000001",
                "sub_product_id": "100500123",
            }
        }
    )
    shape = freight_product_sku_shape(detail, "14:771")
    assert shape["converter_present"] == "yes"
    assert shape["main_product_present"] == "yes"
    assert shape["main_differs_from_base"] == "yes"
    assert shape["sub_product_present"] == "yes"
    assert shape["raw_sku_present"] == "yes"
    assert shape["raw_sku_canonical"] == "yes"
    # A forma NÃO expõe IDs/valores do product/SKU.
    serialized = str(shape)
    assert "2000000001" not in serialized
    assert "100500123" not in serialized
    assert "14" not in serialized
    assert "771" not in serialized


# ----- method/top-level param/assinatura ------------------------------------
def test_get_method_and_top_level_param():
    transport = _FakeTransport(
        '{"aliexpress_logistics_buyer_freight_get_response":{"code":"00"}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    client.get_buyer_freight(product_id="123", sku_id="s1")
    params = transport.last_params
    assert params["method"] == "aliexpress.logistics.buyer.freight.get"
    assert "aeopFreightCalculateForBuyerDTO" in params
    # NÃO usa o param antigo do freight.calculate.
    assert "param_aeop_freight_calculate_for_buyer_d_t_o" not in params
    assert params["session"] == "tok"
    assert params["sign_method"] == "sha256"
    assert "sign" in params
    assert "app_secret" not in params


def test_get_dto_participates_in_signature():
    transport = _FakeTransport(
        '{"aliexpress_logistics_buyer_freight_get_response":{"code":"00"}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    client.get_buyer_freight(product_id="123", sku_id="s1")
    params = transport.last_params
    expected = sign_sync({k: v for k, v in params.items() if k != "sign"}, "s")
    assert params["sign"] == expected


# ----- normalização do wrapper ----------------------------------------------
def _get_wrapped_option(**overrides):
    opt = {
        "shipping_method": "CAINIAO_STANDARD",
        "service_name": "AliExpress Standard Shipping",
        "estimated_delivery_time": "5-8day",
        "freight": {"cent": 1000, "currency_code": "USD", "currency": "USD"},
        "tracking_available": True,
    }
    opt.update(overrides)
    return opt


def test_normalize_get_wrapper_official():
    payload = {
        "aliexpress_logistics_buyer_freight_get_response": {
            "result": {
                "aeop_freight_calculate_result_for_buyer_dtolist": [
                    {"aeop_freight_calculate_result_for_buyer_d_t_o": _get_wrapped_option()}
                ]
            }
        }
    }
    res = normalize_buyer_freight_get_response(
        payload["aliexpress_logistics_buyer_freight_get_response"],
        method="aliexpress.logistics.buyer.freight.get",
    )
    assert len(res.options) == 1
    opt = res.options[0]
    assert opt.service_name == "AliExpress Standard Shipping"
    assert opt.estimated_delivery == "5-8day"
    assert opt.tracking is True
    assert opt.raw  # bruto preservado
    assert res.raw


def test_normalize_get_direct_list_in_intermediate_wrapper():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_dtolist": [
                _get_wrapped_option(freight={"amount": "5.00", "currency_code": "BRL"})
            ]
        }
    }
    res = normalize_buyer_freight_get_response(
        payload, method="aliexpress.logistics.buyer.freight.get"
    )
    assert len(res.options) == 1
    assert res.options[0].shipping_fee == Decimal("5.00")
    assert res.currency == "BRL"


def test_normalize_get_amount_preferred():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_dtolist": [
                _get_wrapped_option(
                    freight={"amount": "1.0", "cent": 1000, "currency_code": "USD"}
                )
            ]
        }
    }
    res = normalize_buyer_freight_get_response(
        payload, method="aliexpress.logistics.buyer.freight.get"
    )
    assert res.options[0].shipping_fee == Decimal("1.0")


def test_normalize_get_cent_only_preserved_in_raw():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_dtolist": [
                _get_wrapped_option()  # cent presente, sem amount
            ]
        }
    }
    res = normalize_buyer_freight_get_response(
        payload, method="aliexpress.logistics.buyer.freight.get"
    )
    opt = res.options[0]
    # Sem amount: ainda não convertemos cent; bruto preservado em raw.
    assert opt.shipping_fee is None
    assert opt.raw["freight"]["cent"] == 1000


def test_normalize_get_business_error_raises():
    transport = _FakeTransport(
        '{"error_response":{"code":27,"msg":"skuId is need",'
        '"sub_msg":"","request_id":"r-1"}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    with pytest.raises(AliExpressAPIError) as excinfo:
        client.get_buyer_freight(product_id="123", sku_id="s1")
    exc = excinfo.value
    assert exc.code == 27
    assert "skuId is need" in exc.message


def test_get_no_secret_in_logs(caplog):
    transport = _FakeTransport(
        '{"aliexpress_logistics_buyer_freight_get_response":{"code":"00"}}'
    )
    client = AliExpressClient(
        app_key="k",
        app_secret="SUPERSECRET",
        access_token="TOKEN123",
        transport=transport,
    )
    with caplog.at_level(logging.INFO):
        client.get_buyer_freight(product_id="123", sku_id="s1")
    log_text = caplog.text
    assert "SUPERSECRET" not in log_text
    assert "TOKEN123" not in log_text