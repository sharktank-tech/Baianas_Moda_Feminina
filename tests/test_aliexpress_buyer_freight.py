"""Testes offline de aliexpress.logistics.buyer.freight.calculate (Fase 4b)."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import AliExpressClient
from loja.integrations.aliexpress.buyer_freight import (
    build_buyer_freight_params,
    normalize_buyer_freight_response,
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


# ----- parâmetros obrigatórios --------------------------------------------
def test_buyer_freight_requires_product_id():
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id="")
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id=None)


def test_buyer_freight_requires_country_and_origin():
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id=123456, country_code="")
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id=123456, send_goods_country_code="")


def test_buyer_freight_product_num_minimum():
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id=123456, product_num=0)
    p = build_buyer_freight_params(product_id=123456, product_num=1)
    assert '"product_num":1' in p["param_aeop_freight_calculate_for_buyer_d_t_o"]


# ----- JSON compacto determinístico ----------------------------------------
def test_buyer_freight_params_json_deterministic():
    p = build_buyer_freight_params(
        product_id=123456789,
        country_code="BR",
        product_num=1,
        send_goods_country_code="CN",
        province_code=None,
        city_code=None,
    )
    assert p["param_aeop_freight_calculate_for_buyer_d_t_o"] == (
        '{"country_code":"BR","product_id":123456789,"product_num":1,'
        '"send_goods_country_code":"CN"}'
    )


def test_buyer_freight_product_id_string_becomes_number():
    p = build_buyer_freight_params(product_id="123")
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"product_id":123' in dto
    assert '"product_id":"123"' not in dto


def test_buyer_freight_product_id_int_stays_number():
    p = build_buyer_freight_params(product_id=123)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"product_id":123' in dto


def test_buyer_freight_product_id_non_numeric_raises():
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id="ABC")
    with pytest.raises(ValueError):
        build_buyer_freight_params(product_id=None)


def test_buyer_freight_product_num_is_number():
    p = build_buyer_freight_params(product_id=123, product_num=2)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"product_num":2' in dto
    assert '"product_num":"2"' not in dto


def test_buyer_freight_sku_id_is_string():
    p = build_buyer_freight_params(product_id=123, sku_id=12345)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"sku_id":"12345"' in dto


def test_buyer_freight_province_city_omitted_when_absent():
    p = build_buyer_freight_params(product_id=123, province_code=None, city_code=None)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert "province_code" not in dto
    assert "city_code" not in dto


def test_buyer_freight_province_city_optional_present():
    p = build_buyer_freight_params(
        product_id=123456,
        country_code="BR",
        send_goods_country_code="CN",
        province_code="SP",
        city_code="SAO_PAULO",
    )
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"province_code":"SP"' in dto
    assert '"city_code":"SAO_PAULO"' in dto


def test_buyer_freight_price_omitted_when_absent():
    p = build_buyer_freight_params(product_id=123456)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert "price" not in dto
    assert "price_currency" not in dto


def test_buyer_freight_price_included_when_both_present():
    p = build_buyer_freight_params(
        product_id=123456, price=Decimal("9.99"), price_currency="USD"
    )
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"price":"9.99"' in dto
    assert '"price_currency":"USD"' in dto


def test_buyer_freight_sku_id_present_in_json():
    p = build_buyer_freight_params(product_id=123456, sku_id="12345")
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"sku_id":"12345"' in dto


def test_buyer_freight_sku_id_omitted_when_absent():
    p = build_buyer_freight_params(product_id=123456)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert "sku_id" not in dto


# ----- assinatura inclui o business param -----------------------------------
def test_buyer_param_participates_in_signature():
    transport = _FakeTransport(
        '{"aliexpress_logistics_buyer_freight_calculate_response":{"code":"00"}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    client.calculate_buyer_freight(product_id=123456)
    params = transport.last_params
    assert "param_aeop_freight_calculate_for_buyer_d_t_o" in params
    expected = sign_sync({k: v for k, v in params.items() if k != "sign"}, "s")
    assert params["sign"] == expected


def test_buyer_freight_via_client_builds_request():
    transport = _FakeTransport(
        '{"aliexpress_logistics_buyer_freight_calculate_response":{"code":"00"}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    res = client.calculate_buyer_freight(product_id=123456, country_code="BR")
    params = transport.last_params
    assert params["method"] == "aliexpress.logistics.buyer.freight.calculate"
    assert params["session"] == "tok"
    assert params["sign_method"] == "sha256"
    assert "sign" in params
    assert "app_secret" not in params
    assert res is not None


# ----- normalização do wrapper oficial --------------------------------------
def test_normalize_buyer_wrapper_official():
    payload = {
        "aliexpress_logistics_buyer_freight_calculate_response": {
            "code": "00",
            "result": {
                "aeop_freight_calculate_result_for_buyer_d_t_o_list": [
                    {
                        "service_name": "Cainiao",
                        "freight": {
                            "amount": "12.50",
                            "cent": "1250",
                            "currency_code": "BRL",
                        },
                        "estimated_delivery_time": 15,
                    },
                    {
                        "service_name": "EMS",
                        "freight": {
                            "amount": "20.00",
                            "cent": "2000",
                            "currency_code": "BRL",
                        },
                        "estimated_delivery_time": 25,
                    },
                ]
            },
        }
    }
    res = normalize_buyer_freight_response(
        payload["aliexpress_logistics_buyer_freight_calculate_response"],
        method="aliexpress.logistics.buyer.freight.calculate",
    )
    assert len(res.options) == 2
    assert res.currency == "BRL"
    assert res.min_fee == Decimal("12.50")
    assert res.min_delivery_days == 15
    opt0 = res.options[0]
    assert opt0.service_name == "Cainiao"
    assert opt0.shipping_fee == Decimal("12.50")  # usa amount, não cent
    assert opt0.currency == "BRL"
    assert opt0.delivery_days == 15
    assert opt0.raw


def test_normalize_buyer_amount_primary_over_cent():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_d_t_o_list": [
                {
                    "service_name": "X",
                    "freight": {"amount": "5.00", "cent": "999", "currency_code": "USD"},
                    "estimated_delivery_time": 10,
                }
            ]
        }
    }
    res = normalize_buyer_freight_response(
        payload, method="aliexpress.logistics.buyer.freight.calculate"
    )
    assert res.options[0].shipping_fee == Decimal("5.00")


def test_normalize_buyer_falls_back_to_cent_when_amount_absent():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_d_t_o_list": [
                {
                    "service_name": "X",
                    "freight": {"cent": "300", "currency_code": "USD"},
                    "estimated_delivery_time": 10,
                }
            ]
        }
    }
    res = normalize_buyer_freight_response(
        payload, method="aliexpress.logistics.buyer.freight.calculate"
    )
    assert res.options[0].shipping_fee == Decimal("300")


# ----- erro de negócio sanitizado -------------------------------------------
def test_buyer_freight_business_error_raises():
    transport = _FakeTransport(
        '{"error_response":{"code":27,"msg":"method not found",'
        '"sub_msg":"","request_id":"r-1"}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    with pytest.raises(AliExpressAPIError) as excinfo:
        client.calculate_buyer_freight(product_id=123456)
    exc = excinfo.value
    assert exc.code == 27
    assert "method not found" in exc.message


# ----- wrapper oficial aninhado (dict -> lista) ------------------------------
def test_normalize_buyer_official_nested_wrapper():
    payload = {
        "aliexpress_logistics_buyer_freight_calculate_response": {
            "result": {
                "aeop_freight_calculate_result_for_buyer_d_t_o_list": {
                    "aeop_freight_calculate_result_for_buyer_dto": [
                        {
                            "error_code": 0,
                            "estimated_delivery_time": "5-8day",
                            "freight": {
                                "amount": "1.0",
                                "cent": 1000,
                                "currency_code": "USD",
                            },
                            "service_name": "CAINIAO_STANDARD",
                        }
                    ]
                },
                "success": True,
            }
        }
    }
    res = normalize_buyer_freight_response(
        payload["aliexpress_logistics_buyer_freight_calculate_response"],
        method="aliexpress.logistics.buyer.freight.calculate",
    )
    assert len(res.options) == 1
    opt = res.options[0]
    assert opt.service_name == "CAINIAO_STANDARD"
    assert opt.shipping_fee == Decimal("1.0")
    assert opt.currency == "USD"
    assert opt.estimated_delivery == "5-8day"


def test_normalize_buyer_direct_list():
    payload = [
        {
            "error_code": 0,
            "service_name": "EMS",
            "freight": {"amount": "9.90", "currency_code": "USD"},
            "estimated_delivery_time": "10-15day",
        }
    ]
    res = normalize_buyer_freight_response(
        payload, method="aliexpress.logistics.buyer.freight.calculate"
    )
    assert len(res.options) == 1
    assert res.options[0].service_name == "EMS"
    assert res.options[0].shipping_fee == Decimal("9.90")


def test_normalize_buyer_success_false_with_error_desc():
    payload = {
        "result": {
            "success": False,
            "error_desc": "DELIVERY_NOT_AVAILABLE",
            "error_code": 505,
        }
    }
    with pytest.raises(AliExpressAPIError) as excinfo:
        normalize_buyer_freight_response(
            payload, method="aliexpress.logistics.buyer.freight.calculate"
        )
    exc = excinfo.value
    assert exc.code == 505
    assert "DELIVERY_NOT_AVAILABLE" in exc.message


def test_normalize_buyer_success_string_false():
    payload = {
        "result": {
            "success": "false",
            "error_desc": "INVALID_ADDRESS",
            "error_code": 501,
        }
    }
    with pytest.raises(AliExpressAPIError) as excinfo:
        normalize_buyer_freight_response(
            payload, method="aliexpress.logistics.buyer.freight.calculate"
        )
    exc = excinfo.value
    assert exc.code == 501
    assert "INVALID_ADDRESS" in exc.message


def test_normalize_buyer_error_code_zero_accepted():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_d_t_o_list": [
                {
                    "error_code": 0,
                    "service_name": "Cainiao",
                    "freight": {"amount": "5.00", "currency_code": "BRL"},
                    "estimated_delivery_time": 12,
                }
            ],
            "success": True,
        }
    }
    res = normalize_buyer_freight_response(
        payload, method="aliexpress.logistics.buyer.freight.calculate"
    )
    assert len(res.options) == 1
    assert res.options[0].shipping_fee == Decimal("5.00")


def test_normalize_buyer_all_options_errored_raises():
    payload = {
        "result": {
            "aeop_freight_calculate_result_for_buyer_d_t_o_list": [
                {
                    "error_code": 505,
                    "service_name": "Cainiao",
                    "freight": {"amount": "5.00", "currency_code": "BRL"},
                },
                {
                    "error_code": 501,
                    "service_name": "EMS",
                    "freight": {"amount": "9.00", "currency_code": "BRL"},
                },
            ],
            "success": True,
        }
    }
    with pytest.raises(AliExpressAPIError) as excinfo:
        normalize_buyer_freight_response(
            payload, method="aliexpress.logistics.buyer.freight.calculate"
        )
    assert "erro" in excinfo.value.message.lower()


# ----- nenhum segredo em logs -----------------------------------------------
def test_buyer_freight_no_secret_in_logs(caplog):
    transport = _FakeTransport(
        '{"aliexpress_logistics_buyer_freight_calculate_response":{"code":"00"}}'
    )
    client = AliExpressClient(
        app_key="k",
        app_secret="SUPERSECRET",
        access_token="TOKEN123",
        transport=transport,
    )
    with caplog.at_level(logging.INFO):
        client.calculate_buyer_freight(product_id=123456)
    log_text = caplog.text
    assert "SUPERSECRET" not in log_text
    assert "TOKEN123" not in log_text
