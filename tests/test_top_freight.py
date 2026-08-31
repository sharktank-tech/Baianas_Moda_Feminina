"""Testes offline do transporte TOP legado para buyer.freight.calculate (Fase 4b).

Somente o frete usa https://eco.taobao.com/router/rest com sign_top (md5) e
timestamp GMT+8; search/product.get continuam no /sync (Alterado fora daqui).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from loja.integrations.aliexpress import (
    TOP_ROUTER_ENDPOINT,
    build_top_freight_calculate_dto,
    build_top_freight_params,
    call_top_freight_calculate,
    sign_top_freight_params,
    top_gmt8_timestamp,
)
from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.sign import sign_sync, sign_top
from loja.integrations.aliexpress.transport import TransportError

APP = dict(
    app_key="k",
    access_token="TOKEN123",
    product_id="123456",
    raw_sku_id="14:771",
    send_goods_country_code="CN",
    country_code="BR",
    product_num=1,
    timestamp="2026-08-30 18:00:00",
)
APP_SECRET = "SECRET123"


# ----- endpoint TOP ---------------------------------------------------------
def test_top_endpoint_is_taobao_router_rest():
    assert TOP_ROUTER_ENDPOINT == "https://eco.taobao.com/router/rest"


# ----- timestamp GMT+8 no formato TOP ---------------------------------------
def test_timestamp_format_top():
    ts = top_gmt8_timestamp(datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc))
    assert ts == "2026-08-30 18:00:00"


def test_timestamp_rolls_over_date_at_gmt8():
    ts = top_gmt8_timestamp(datetime(2026, 8, 30, 16, 0, 0, tzinfo=timezone.utc))
    assert ts == "2026-08-31 00:00:00"
    ts = top_gmt8_timestamp(datetime(2026, 8, 30, 23, 59, 59, tzinfo=timezone.utc))
    assert ts == "2026-08-31 07:59:59"


# ----- parâmetros públicos TOP ----------------------------------------------
def test_top_params_public_fields():
    p = build_top_freight_params(**APP)
    assert p["method"] == "aliexpress.logistics.buyer.freight.calculate"
    assert p["app_key"] == "k"
    assert p["session"] == "TOKEN123"  # session recebe o access token
    assert p["timestamp"] == "2026-08-30 18:00:00"
    assert p["format"] == "json"
    assert p["v"] == "2.0"
    assert p["sign_method"] == "md5"
    assert "sign" not in p  # o sign é adicionado depois


def test_top_session_receives_access_token():
    p = build_top_freight_params(**APP)
    assert p["session"] == "TOKEN123"


# ----- DTO mínimo (skuId camelCase, sem extras) -----------------------------
def test_top_dto_has_skuId_not_sku_id():
    p = build_top_freight_params(**APP)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert '"skuId":"14:771"' in dto
    assert "sku_id" not in dto


def test_top_dto_numbers_and_omits_extras():
    p = build_top_freight_params(**APP)
    dto = p["param_aeop_freight_calculate_for_buyer_d_t_o"]
    assert dto == (
        '{"country_code":"BR","product_id":123456,"product_num":1,'
        '"send_goods_country_code":"CN","skuId":"14:771"}'
    )
    assert '"product_id":"123456"' not in dto
    assert '"product_num":"1"' not in dto
    assert "province_code" not in dto
    assert "city_code" not in dto
    assert "price" not in dto
    assert "price_currency" not in dto


def test_top_dto_builder_requires_numeric_product_id():
    with pytest.raises(ValueError):
        build_top_freight_calculate_dto(product_id="ABC", raw_sku_id="14:771")
    with pytest.raises(ValueError):
        build_top_freight_params(
            app_key="k", access_token="t", product_id=None, raw_sku_id="14:771"
        )
    with pytest.raises(ValueError):
        build_top_freight_params(
            app_key="k", access_token="t", product_id="123", raw_sku_id="14:771",
            product_num=0,
        )


# ----- assinatura md5 via sign_top existente --------------------------------
def test_top_sign_uses_md5_sign_top():
    p = build_top_freight_params(**APP)
    sign_top_freight_params(p, APP_SECRET)
    assert "sign" in p
    expected = sign_top({k: v for k, v in p.items() if k != "sign"}, APP_SECRET, method="md5")
    assert p["sign"] == expected
    # NÃO é a assinatura sha256 do /sync.
    assert p["sign"] != sign_sync({k: v for k, v in p.items() if k != "sign"}, APP_SECRET)


def test_top_sign_never_includes_app_secret():
    p = build_top_freight_params(**APP)
    sign_top_freight_params(p, APP_SECRET)
    assert "app_secret" not in p
    assert APP_SECRET not in json.dumps(p)


# ----- POST form-urlencoded no router TOP -----------------------------------
def _http_resp(status, content_type, body):
    class _Resp:
        status_code = status
        headers = {"Content-Type": content_type}
        content = body.encode("utf-8")
        text = body

    return _Resp()


def test_top_post_uses_router_endpoint_and_form_urlencoded(monkeypatch):
    import requests

    seen = {}
    body = '{"aliexpress_logistics_buyer_freight_calculate_response":{"code":"00"}}'

    def _fake_post(url, data=None, timeout=None, headers=None):
        seen["url"] = url
        seen["data"] = dict(data)
        seen["headers"] = headers
        return _http_resp(200, "application/json", body)

    monkeypatch.setattr(requests, "post", _fake_post)
    res = call_top_freight_calculate(**APP, app_secret=APP_SECRET)
    assert res is not None
    assert seen["url"] == "https://eco.taobao.com/router/rest"
    assert seen["data"]["method"] == "aliexpress.logistics.buyer.freight.calculate"
    assert seen["data"]["session"] == "TOKEN123"
    assert seen["data"]["sign_method"] == "md5"
    assert "sign" in seen["data"]
    assert "app_secret" not in seen["data"]
    ct = seen["headers"]["Content-Type"]
    assert ct.startswith("application/x-www-form-urlencoded")


def test_top_post_non_json_raises_transport_error(monkeypatch):
    import requests

    def _fake_post(url, data=None, timeout=None, headers=None):
        return _http_resp(200, "text/html", "<html>waf</html>")

    monkeypatch.setattr(requests, "post", _fake_post)
    with pytest.raises(TransportError):
        call_top_freight_calculate(**APP, app_secret=APP_SECRET)


def test_top_post_http_error_raises_transport_error(monkeypatch):
    import requests

    def _fake_post(url, data=None, timeout=None, headers=None):
        return _http_resp(401, "application/json", '{"error_response":{"code":1}}')

    monkeypatch.setattr(requests, "post", _fake_post)
    with pytest.raises(TransportError):
        call_top_freight_calculate(**APP, app_secret=APP_SECRET)


# ----- normalização da resposta oficial -------------------------------------
def _ok_payload():
    return json.dumps(
        {
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
                            "error_code": 0,
                        }
                    ]
                },
            }
        }
    )


def test_top_call_normalizes_success():
    res = call_top_freight_calculate(**APP, app_secret=APP_SECRET, post=lambda params: _ok_payload())
    assert len(res.options) == 1
    assert res.min_fee == Decimal("12.50")
    assert res.min_delivery_days == 15
    assert res.currency == "BRL"


def test_top_call_converts_error_response():
    def _err_post(params):
        return json.dumps(
            {
                "error_response": {
                    "code": 15,
                    "msg": "Remote service error",
                    "sub_code": "isp.not-signed",
                    "request_id": "r-9",
                }
            }
        )

    with pytest.raises(AliExpressAPIError) as excinfo:
        call_top_freight_calculate(**APP, app_secret=APP_SECRET, post=_err_post)
    exc = excinfo.value
    assert exc.code == 15
    assert "Remote service error" in exc.message
    assert exc.sub_code == "isp.not-signed"


def test_top_call_non_json_body_raises_api_error():
    with pytest.raises(AliExpressAPIError):
        call_top_freight_calculate(**APP, app_secret=APP_SECRET, post=lambda params: "<html>oops</html>")


# ----- nenhum segredo em logs -----------------------------------------------
def test_top_no_secret_in_logs(caplog):
    import logging

    with caplog.at_level(logging.INFO):
        call_top_freight_calculate(**APP, app_secret=APP_SECRET, post=lambda params: _ok_payload())
    log_text = caplog.text
    assert "SECRET123" not in log_text
    assert "TOKEN123" not in log_text
    assert "14:771" not in log_text
    assert "123456" not in log_text