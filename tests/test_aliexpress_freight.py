"""Testes offline de aliexpress.ds.freight.query (Fase 4, sem rede)."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import AliExpressClient
from loja.integrations.aliexpress.freight import (
    build_freight_params,
    describe_shape,
    normalize_freight_response,
)
from loja.integrations.aliexpress.sign import sign_sync


class _FakeTransport:
    def __init__(self, body: str = "{}"):
        self.body = body
        self.last_params: dict = {}

    def post(self, params: dict) -> str:
        self.last_params = dict(params)
        return self.body


# ----- validação de parâmetros ---------------------------------------------
def test_freight_requires_selected_sku():
    with pytest.raises(ValueError):
        build_freight_params(product_id="P1", selected_sku_id="")
    with pytest.raises(ValueError):
        build_freight_params(product_id="P1", selected_sku_id=None)


def test_freight_requires_product_id():
    with pytest.raises(ValueError):
        build_freight_params(product_id="", selected_sku_id="S1")


def test_freight_requires_country_and_currency():
    with pytest.raises(ValueError):
        build_freight_params(product_id="P1", selected_sku_id="S1", ship_to_country="")
    with pytest.raises(ValueError):
        build_freight_params(product_id="P1", selected_sku_id="S1", currency="")


def test_freight_quantity_minimum():
    with pytest.raises(ValueError):
        build_freight_params(product_id="P1", selected_sku_id="S1", quantity=0)
    p = build_freight_params(product_id="P1", selected_sku_id="S1", quantity=1)
    assert '"quantity":1' in p["queryDeliveryReq"]


# ----- JSON compacto determinístico ----------------------------------------
def test_freight_params_json_deterministic():
    p = build_freight_params(product_id="P1", selected_sku_id="S1")
    assert p["queryDeliveryReq"] == (
        '{"productId":"P1","selectedSkuId":"S1","shipToCountry":"BR",'
        '"provinceCode":"","cityCode":"","currency":"BRL","quantity":1,'
        '"source":"","language":"en_US","locale":""}'
    )


# ----- assinatura inclui queryDeliveryReq -----------------------------------
def test_query_delivery_req_participates_in_signature():
    transport = _FakeTransport('{"aliexpress_ds_freight_query_response":{"code":"00"}}')
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    client.query_freight(product_id="P1", selected_sku_id="S1")
    params = transport.last_params
    assert "queryDeliveryReq" in params
    # Prova que queryDeliveryReq entrou no material assinado.
    expected = sign_sync({k: v for k, v in params.items() if k != "sign"}, "s")
    assert params["sign"] == expected


def test_query_freight_via_client_builds_request():
    transport = _FakeTransport(
        '{"aliexpress_ds_freight_query_response":{"code":"00",'
        '"freightList":[{"serviceName":"X","freightFee":"5.00","currency":"BRL","deliveryDays":10}]}}'
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    res = client.query_freight(product_id="P1", selected_sku_id="S1", ship_to_country="BR")

    params = transport.last_params
    assert params["method"] == "aliexpress.ds.freight.query"
    assert params["session"] == "tok"
    assert params["sign_method"] == "sha256"
    assert "sign" in params
    assert "app_secret" not in params  # segredo NUNCA viaja nos parâmetros

    assert res.min_fee == Decimal("5.00")
    assert res.min_delivery_days == 10


# ----- normalização tolerante (wrappers / lista / opção única) -------------
def test_normalize_freight_list_wrapper():
    payload = {
        "aliexpress_ds_freight_query_response": {
            "code": "00",
            "result": {
                "freightList": [
                    {
                        "serviceName": "Cainiao",
                        "company": "AE Standard",
                        "freightFee": "12.50",
                        "currency": "BRL",
                        "deliveryDays": 15,
                        "tracking": True,
                    },
                    {
                        "serviceName": "EMS",
                        "freightFee": "20.00",
                        "currency": "BRL",
                        "deliveryDays": 25,
                    },
                ]
            },
        }
    }
    res = normalize_freight_response(
        payload["aliexpress_ds_freight_query_response"],
        method="aliexpress.ds.freight.query",
    )
    assert len(res.options) == 2
    assert res.currency == "BRL"
    assert res.min_fee == Decimal("12.50")
    assert res.min_delivery_days == 15
    assert res.options[0].shipping_fee == Decimal("12.50")
    assert res.options[0].tracking is True
    assert res.options[0].raw  # preservado
    assert res.raw  # payload inteiro preservado


def test_normalize_freight_single_option_dict():
    payload = {
        "serviceName": "Cainiao",
        "freightFee": "12.50",
        "currency": "BRL",
        "deliveryDays": 15,
    }
    res = normalize_freight_response(payload, method="aliexpress.ds.freight.query")
    assert len(res.options) == 1
    assert res.options[0].shipping_fee == Decimal("12.50")
    assert res.options[0].raw == payload


def test_normalize_freight_direct_list():
    payload = {
        "freightList": [
            {"serviceName": "X", "freightFee": "5.00", "currency": "BRL", "deliveryDays": 10}
        ]
    }
    res = normalize_freight_response(payload, method="aliexpress.ds.freight.query")
    assert len(res.options) == 1
    assert res.min_fee == Decimal("5.00")
    assert res.min_delivery_days == 10


# ----- nenhum segredo em logs ----------------------------------------------
def test_query_freight_no_secret_in_logs(caplog):
    transport = _FakeTransport('{"aliexpress_ds_freight_query_response":{"code":"00"}}')
    client = AliExpressClient(
        app_key="k",
        app_secret="SUPERSECRET",
        access_token="TOKEN123",
        transport=transport,
    )
    with caplog.at_level(logging.INFO):
        client.query_freight(product_id="P1", selected_sku_id="S1")
    log_text = caplog.text
    assert "SUPERSECRET" not in log_text
    assert "TOKEN123" not in log_text


# ----- shape live (aliases) ------------------------------------------------
def _freight_item():
    return {
        "code": "CAINIAO_STANDARD",
        "shipping_fee_currency": "USD",
        "free_shipping": False,
        "guaranteed_delivery_days": 60,
        "max_delivery_days": 19,
        "tracking": True,
        "shipping_fee_format": "US $1.82",
        "delivery_date_desc": "May 05 - 14",
        "company": "AliExpress standard shipping",
        "ship_from_country": "CN",
        "min_delivery_days": 10,
        "shipping_fee_cent": "1.82",
    }


def _assert_live_option(res):
    assert len(res.options) == 1
    opt = res.options[0]
    assert opt.shipping_fee == Decimal("1.82")
    assert opt.currency == "USD"
    assert opt.delivery_days == 10  # prefere min_delivery_days
    assert opt.estimated_delivery == "May 05 - 14"
    assert opt.tracking is True
    assert opt.country == "CN"
    assert res.min_fee == Decimal("1.82")
    assert res.min_delivery_days == 10
    # Campos extras preservados em raw (não inventados).
    assert opt.raw.get("max_delivery_days") == 19
    assert opt.raw.get("guaranteed_delivery_days") == 60
    assert opt.raw.get("free_shipping") is False
    assert "shipping_fee_cent" in opt.raw


def test_normalize_freight_live_shape_freight_list():
    # Forma live: {"freight": [ {...} ]}
    payload = {"freight": [_freight_item()]}
    res = normalize_freight_response(payload, method="aliexpress.ds.freight.query")
    _assert_live_option(res)


def test_normalize_freight_live_shape_result_delivery_options():
    payload = {"result": {"delivery_options": [_freight_item()]}}
    res = normalize_freight_response(payload, method="aliexpress.ds.freight.query")
    _assert_live_option(res)


def test_normalize_freight_live_shape_data_freight():
    payload = {"data": {"freight": [_freight_item()]}}
    res = normalize_freight_response(payload, method="aliexpress.ds.freight.query")
    _assert_live_option(res)


def test_describe_shape_omits_values():
    shape = describe_shape({"code": "00", "data": {"delivery_options": [{"company": "X"}]}})
    text = "\n".join(shape)
    # Apenas chaves/tipos/tamanhos; NUNCA valores.
    assert "code" in text
    assert "delivery_options" in text
    assert "company" in text
    assert "00" not in text  # valor não vaza
    assert "X" not in text
