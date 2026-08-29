"""Testes offline de aliexpress.ds.freight.query (Fase 4, sem rede)."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import AliExpressClient
from loja.integrations.aliexpress.freight import (
    build_freight_params,
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
