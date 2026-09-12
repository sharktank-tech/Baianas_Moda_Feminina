"""FASE 8.0.11U/8.0.11W — contrato do placeorder migrado para /sync.

Todos os testes são locais, com transporte injetado. Zero HTTP real.
"""

from __future__ import annotations

import json
import uuid

import pytest

from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.placeorder_provider import (
    TopAliExpressOrderSubmitter,
)
from loja.integrations.aliexpress.sign import sign_sync
from loja.integrations.aliexpress.submission import (
    AliExpressPlaceOrderItem,
    AliExpressSubmissionRequest,
    AliExpressSubmissionUnknown,
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


def _request():
    return AliExpressSubmissionRequest(
        aliexpress_order_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        attempt_key="attempt-u",
        shipping_address={
            "contact_person": "Cliente",
            "mobile_no": "11999999999",
            "zip": "01001000",
            "address": "Rua Exemplo",
            "city": "São Paulo",
            "province": "SP",
            "country": "BR",
        },
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


def test_parse_sync_failure_with_empty_order_list_remains_business_failure():
    result = parse_sync_placeorder_response(
        {
            "aliexpress_trade_buy_placeorder_response": {
                "result": {
                    "is_success": False,
                    "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL",
                    "order_list": {"number": []},
                }
            }
        }
    )
    assert result.is_success is False
    assert result.order_ids == ()
    assert result.error_code == "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"


@pytest.mark.parametrize(
    "order_list",
    [
        {"number": [12345]},
        {"number": 12345},
        [12345],
        "12345",
    ],
)
def test_parse_sync_failure_with_remote_identity_fails_closed(order_list):
    with pytest.raises(AliExpressAPIError) as exc:
        parse_sync_placeorder_response(
            {
                "aliexpress_trade_buy_placeorder_response": {
                    "result": {
                        "is_success": False,
                        "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL",
                        "order_list": order_list,
                    }
                }
            }
        )
    assert str(exc.value) == "resposta placeorder contraditoria"
    assert "12345" not in str(exc.value)


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


def test_provider_contradictory_failure_identity_becomes_unknown(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_PLACEORDER_ENABLED", "1")

    def post(params):
        return json.dumps(
            {
                "aliexpress_trade_buy_placeorder_response": {
                    "result": {
                        "is_success": False,
                        "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL",
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
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        provider.submit(_request())
    assert exc.value.args[0] == "placeorder_protocol_error"


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
