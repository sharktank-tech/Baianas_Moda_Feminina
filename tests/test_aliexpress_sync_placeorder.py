"""FASE 8.0.11U/8.0.11X — contrato do placeorder em /sync.

Todos os testes são locais, com transporte injetado. Zero HTTP real.
"""

from __future__ import annotations

import json
import uuid

import pytest

from loja.integrations.aliexpress.placeorder_provider import TopAliExpressOrderSubmitter
from loja.integrations.aliexpress.sign import sign_sync
from loja.integrations.aliexpress.submission import (
    AliExpressPlaceOrderItem,
    AliExpressSubmissionRequest,
)
from loja.integrations.aliexpress.sync_placeorder import (
    SYNC_PLACEORDER_BUSINESS_PARAM,
    SYNC_PLACEORDER_ENDPOINT,
    SYNC_PLACEORDER_METHOD,
    build_sync_placeorder_params,
    call_sync_placeorder,
    parse_sync_placeorder_response,
    sign_sync_placeorder_params,
)


def _request(*, extra_shipping=None):
    shipping = {
        "contact_person": "Cliente",
        "mobile_no": "11999999999",
        "zip": "01001000",
        "address": "Rua Exemplo 123",
        "city": "São Paulo",
        "province": "SP",
        "country": "BR",
    }
    shipping.update(extra_shipping or {})
    return AliExpressSubmissionRequest(
        aliexpress_order_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        attempt_key="attempt-u",
        shipping_address=shipping,
        items=(
            AliExpressPlaceOrderItem(
                product_id="123456789",
                sku_attr="14:771;200000124:100013888",
                product_count=1,
            ),
        ),
    )


def test_sync_endpoint_is_modern_open_platform():
    assert SYNC_PLACEORDER_ENDPOINT == "https://api-sg.aliexpress.com/sync"
    assert "eco.taobao.com" not in SYNC_PLACEORDER_ENDPOINT


def test_sync_params_contract():
    params = build_sync_placeorder_params(
        app_key="app-key",
        access_token="token",
        request=_request(),
        timestamp="1789150000000",
    )
    assert params["method"] == SYNC_PLACEORDER_METHOD
    assert params["app_key"] == "app-key"
    assert params["session"] == "token"
    assert params["timestamp"] == "1789150000000"
    assert params["sign_method"] == "sha256"
    assert params["simplify"] == "true"
    assert "sign" not in params
    dto = json.loads(params[SYNC_PLACEORDER_BUSINESS_PARAM])
    assert dto["product_items"][0]["product_id"] == 123456789
    assert dto["product_items"][0]["product_count"] == 1


def test_sync_preserves_supported_optional_shipping_fields_only():
    params = build_sync_placeorder_params(
        app_key="app-key",
        access_token="token",
        request=_request(
            extra_shipping={
                "address2": "Apto 10 - Centro",
                "full_name": "Cliente Teste",
                "phone_country": "+55",
                "cpf": "must-not-pass-this-phase",
                "tax_number": "must-not-pass-this-phase",
            }
        ),
        timestamp="1789150000000",
    )
    dto = json.loads(params[SYNC_PLACEORDER_BUSINESS_PARAM])
    shipping = dto["logistics_address"]
    assert shipping["address2"] == "Apto 10 - Centro"
    assert shipping["full_name"] == "Cliente Teste"
    assert shipping["phone_country"] == "+55"
    assert "cpf" not in shipping
    assert "tax_number" not in shipping


def test_sync_omits_blank_optional_shipping_fields():
    params = build_sync_placeorder_params(
        app_key="app-key",
        access_token="token",
        request=_request(
            extra_shipping={"address2": " ", "full_name": "", "phone_country": None}
        ),
        timestamp="1789150000000",
    )
    shipping = json.loads(params[SYNC_PLACEORDER_BUSINESS_PARAM])["logistics_address"]
    assert "address2" not in shipping
    assert "full_name" not in shipping
    assert "phone_country" not in shipping


def test_sync_sign_matches_existing_signer():
    params = build_sync_placeorder_params(
        app_key="app-key",
        access_token="token",
        request=_request(),
        timestamp="1789150000000",
    )
    expected = sign_sync(dict(params), "secret")
    sign_sync_placeorder_params(params, "secret")
    assert params["sign"] == expected


def test_parse_sync_success_single_id():
    payload = {
        "aliexpress_trade_buy_placeorder_response": {
            "result": {"is_success": True, "order_list": {"number": [12345]}}
        }
    }
    result = parse_sync_placeorder_response(payload)
    assert result.is_success is True
    assert result.order_ids == ("12345",)


def test_parse_sync_error_response_preserves_codes():
    result = parse_sync_placeorder_response(
        {
            "error_response": {
                "code": 50,
                "sub_code": "isv.invalid-parameter",
                "msg": "must never be logged",
            }
        }
    )
    assert result.is_success is False
    assert result.error_code == "50"
    assert result.error_sub_code == "isv.invalid-parameter"


def test_call_sync_placeorder_is_exactly_one_logical_post():
    calls = []

    def post(params):
        calls.append(dict(params))
        return json.dumps(
            {
                "aliexpress_trade_buy_placeorder_response": {
                    "result": {
                        "is_success": True,
                        "order_list": {"number": 67890},
                    }
                }
            }
        )

    result = call_sync_placeorder(
        app_key="key",
        app_secret="secret",
        access_token="token",
        request=_request(),
        timestamp="1789150000000",
        post=post,
    )
    assert len(calls) == 1
    assert calls[0]["method"] == SYNC_PLACEORDER_METHOD
    assert calls[0]["sign_method"] == "sha256"
    assert calls[0]["sign"]
    assert result.order_ids == ("67890",)


def test_provider_uses_sync_transport_with_injected_post(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")
    calls = []

    def post(params):
        calls.append(dict(params))
        return json.dumps(
            {
                "aliexpress_trade_buy_placeorder_response": {
                    "result": {
                        "is_success": True,
                        "order_list": {"number": [24680]},
                    }
                }
            }
        )

    provider = TopAliExpressOrderSubmitter(
        app_key="key",
        app_secret="secret",
        resolve_access_token=lambda supplier_id: "token",
        post=post,
    )
    result = provider.submit(_request())
    assert len(calls) == 1
    assert calls[0]["method"] == SYNC_PLACEORDER_METHOD
    assert calls[0]["sign_method"] == "sha256"
    assert result.external_order_ids == ("24680",)


def test_default_provider_still_requires_hardoff_gate(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "0")
    provider = TopAliExpressOrderSubmitter(
        app_key="key",
        app_secret="secret",
        resolve_access_token=lambda supplier_id: "token",
        post=lambda params: pytest.fail("post must not run with gate off"),
    )
    from loja.integrations.aliexpress.submission import SubmissionHardOffError

    with pytest.raises(SubmissionHardOffError):
        provider.submit(_request())
