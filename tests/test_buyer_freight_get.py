"""Testes offline de aliexpress.logistics.buyer.freight.get (Fase 4b isolada)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import (
    AliExpressClient,
    build_buyer_freight_get_params,
    normalize_buyer_freight_get_response,
)
from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.sign import sign_sync


class _FakeTransport:
    def __init__(self, body: str = "{}"):
        self.body = body
        self.last_params: dict = {}

    def post(self, params: dict) -> str:
        self.last_params = dict(params)
        return self.body


# ----- parâmetros obrigatórios ---------------------------------------------
def test_get_requires_product_id_and_sku():
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id="", sku_id="s1")
    with pytest.raises(ValueError):
        build_buyer_freight_get_params(product_id=123, sku_id="")


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
def test_get_dto_json_types():
    p = build_buyer_freight_get_params(product_id=123, sku_id="s1")
    dto = json.loads(p["aeopFreightCalculateForBuyerDTO"])
    assert dto["product_id"] == 123
    assert isinstance(dto["product_id"], int)
    assert dto["product_num"] == 1
    assert isinstance(dto["product_num"], int)
    assert dto["sku_id"] == "s1"
    assert isinstance(dto["sku_id"], str)
    assert dto["country_code"] == "BR"
    assert dto["send_goods_country_code"] == "CN"


def test_get_product_id_string_becomes_number():
    p = build_buyer_freight_get_params(product_id="123", sku_id="s1")
    dto = p["aeopFreightCalculateForBuyerDTO"]
    assert '"product_id":123' in dto


def test_get_province_city_omitted_when_empty():
    p = build_buyer_freight_get_params(product_id=123, sku_id="s1")
    assert "province_code" not in p["aeopFreightCalculateForBuyerDTO"]
    assert "city_code" not in p["aeopFreightCalculateForBuyerDTO"]


def test_get_province_city_included_when_set():
    p = build_buyer_freight_get_params(
        product_id=123, sku_id="s1", province_code="SP", city_code="SAO_PAULO"
    )
    dto = p["aeopFreightCalculateForBuyerDTO"]
    assert '"province_code":"SP"' in dto
    assert '"city_code":"SAO_PAULO"' in dto


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