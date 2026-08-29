"""Testes offline da busca AliExpress (Fase 2) — sem chamadas de rede."""

from __future__ import annotations

import pytest
from decimal import Decimal

from loja.integrations.aliexpress import (
    AliExpressAPIError,
    AliExpressClient,
    AliExpressConfigError,
    AliExpressSearchResult,
    parse_price,
)
from loja.integrations.aliexpress.search import SUPPORTED_SORT


class _CaptureTransport:
    def __init__(self, body: str):
        self.body = body
        self.params: dict = {}

    def post(self, params: dict) -> str:
        self.params = dict(params)
        return self.body


def _client(body: str, **kwargs) -> tuple[AliExpressClient, _CaptureTransport]:
    transport = _CaptureTransport(body)
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport, **kwargs
    )
    return client, transport


SUCCESS = (
    '{"aliexpress.ds.text.search_response": {"code":0, "result": {"total": 2, '
    '"products": [{"itemId":"123","title":"Camiseta","salePrice":"10.90",'
    '"originalPrice":"20.00","imageUrl":"http://img/1.jpg","orders":5},'
    '{"itemId":"456","title":"Top"}]}}}'
)

EMPTY = (
    '{"aliexpress.ds.text.search_response": {"code":0, "result": {"total": 0, '
    '"products": []}}}'
)

ERROR = '{"error_response": {"code": 20020, "msg": "invalid param", "sub_code": "x"}}'


# ----- construção de parâmetros -----
def test_default_br_params():
    client, cap = _client(SUCCESS)
    res = client.search_products()
    assert isinstance(res, AliExpressSearchResult)
    p = cap.params
    assert p["local"] == "en_US"
    assert p["countryCode"] == "BR"
    assert p["currency"] == "BRL"
    assert p["pageIndex"] == "1"
    assert p["pageSize"] == "20"
    assert p["sortBy"] == "orders,desc"
    assert "keyWord" not in p  # keyword opcional ausente
    assert p["session"] == "tok"  # token enviado como session
    assert p["sign"]


def test_keyword_optional():
    client, cap = _client(SUCCESS)
    client.search_products(keyword="t shirt")
    assert cap.params["keyWord"] == "t shirt"


def test_category_id_optional():
    client, cap = _client(SUCCESS)
    client.search_products(category_id="789")
    assert cap.params["categoryId"] == "789"


def test_pagination():
    client, cap = _client(SUCCESS)
    client.search_products(page_index=2, page_size=10)
    assert cap.params["pageIndex"] == "2"
    assert cap.params["pageSize"] == "10"


def test_all_supported_sort_by():
    client, cap = _client(SUCCESS)
    for s in SUPPORTED_SORT:
        client.search_products(sort_by=s)
        assert cap.params["sortBy"] == s


def test_invalid_sort_by_raises():
    client, _ = _client(SUCCESS)
    with pytest.raises(AliExpressConfigError):
        client.search_products(sort_by="price,asc")  # não suportado


def test_missing_country_code_raises():
    client, _ = _client(SUCCESS)
    with pytest.raises(AliExpressConfigError):
        client.search_products(country_code="")


def test_missing_locale_raises():
    client, _ = _client(SUCCESS)
    with pytest.raises(AliExpressConfigError):
        client.search_products(locale="")


def test_invalid_currency_raises():
    client, _ = _client(SUCCESS)
    with pytest.raises(AliExpressConfigError):
        client.search_products(currency="brl")  # minúsculo


def test_page_index_zero_raises():
    client, _ = _client(SUCCESS)
    with pytest.raises(AliExpressConfigError):
        client.search_products(page_index=0)


def test_page_size_zero_raises():
    client, _ = _client(SUCCESS)
    with pytest.raises(AliExpressConfigError):
        client.search_products(page_size=0)


# ----- normalização -----
def test_normalize_complete_product():
    from loja.integrations.aliexpress import normalize_search_response

    payload = {
        "aliexpress.ds.text.search_response": {
            "code": 0,
            "result": {
                "total": 1,
                "products": [
                    {
                        "itemId": "123",
                        "productId": "123",
                        "title": "Camiseta",
                        "productUrl": "http://x/123",
                        "imageUrl": ["http://img/1.jpg", "http://img/2.jpg"],
                        "originalPrice": "20.00",
                        "salePrice": "10.90",
                        "discount": "50%",
                        "orders": 150,
                        "score": "4.8",
                        "evaluateRate": "95%",
                        "categoryId": "123",
                        "videoUrl": "http://vid/1",
                    }
                ],
            },
        }
    }
    res = normalize_search_response(
        payload["aliexpress.ds.text.search_response"], method="aliexpress.ds.text.search"
    )
    assert res.total == 1
    prod = res.products[0]
    assert prod.item_id == "123"
    assert prod.external_id == "123"
    assert prod.title == "Camiseta"
    assert prod.product_url == "http://x/123"
    assert prod.images == ["http://img/1.jpg", "http://img/2.jpg"]
    assert prod.original_price == Decimal("20.00")
    assert prod.sale_price == Decimal("10.90")
    assert prod.discount == "50%"
    assert prod.orders == 150
    assert prod.score == Decimal("4.8")
    assert prod.evaluate_rate == "95%"
    assert prod.category_id == "123"
    assert prod.video_url == "http://vid/1"
    assert isinstance(prod.raw, dict)


def test_normalize_missing_optional_fields_is_none_not_invented():
    from loja.integrations.aliexpress import normalize_search_response

    payload = {"products": [{"itemId": "1"}]}
    res = normalize_search_response(payload, method="aliexpress.ds.text.search")
    prod = res.products[0]
    assert prod.item_id == "1"
    assert prod.title is None
    assert prod.product_url is None
    assert prod.images == []  # lista vazia ok (estrutura), mas sem inventar
    assert prod.original_price is None
    assert prod.sale_price is None
    assert prod.orders is None
    assert prod.score is None
    assert prod.category_id is None


# ----- parsing de preço -----
def test_parse_price_decimal():
    assert parse_price("10.90") == Decimal("10.90")
    assert parse_price("R$ 12,90") == Decimal("12.90")  # símbolo + vírgula
    assert parse_price("1,299.99") == Decimal("1299.99")
    assert parse_price("1.299,99") == Decimal("1299.99")
    assert parse_price(10) == Decimal("10")
    assert parse_price(10.5) == Decimal("10.5")
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("grátis") is None  # não inventa 0


# ----- envelopes -----
def test_empty_response_is_valid_not_error():
    client, _ = _client(EMPTY)
    res = client.search_products()
    assert isinstance(res, AliExpressSearchResult)
    assert res.products == []
    assert res.total == 0


def test_error_envelope_raises_not_empty():
    client, _ = _client(ERROR)
    with pytest.raises(AliExpressAPIError):
        client.search_products()


def test_secrets_redacted_in_error():
    body = '{"error_response": {"code": 20020, "msg": "fail", "session": "SECRETTOKEN"}}'
    client, _ = _client(body)
    with pytest.raises(AliExpressAPIError) as exc:
        client.search_products()
    assert "SECRETTOKEN" not in repr(exc.value)
    assert "SECRETTOKEN" not in str(exc.value)


# ----- resposta live (gateway /sync, code "00", envelope "data") -----
LIVE = (
    '{'
    '  "aliexpress_ds_text_search_response": {'
    '    "code": "00",'
    '    "data": {'
    '      "pageIndex": 1,'
    '      "pageSize": 5,'
    '      "totalCount": 1,'
    '      "products": {'
    '        "selection_search_product": ['
    '          {'
    '            "itemId": "100500123",'
    '            "title": "Test product",'
    '            "itemMainPic": "//example.com/a.jpg",'
    '            "itemUrl": "//www.aliexpress.com/item/100500123.html",'
    '            "targetSalePrice": "19.90",'
    '            "targetOriginalPrice": "29.90",'
    '            "orders": "10,000+",'
    '            "score": "4.8",'
    '            "evaluateRate": "97.5"'
    '          }'
    '        ]'
    '      }'
    '    }'
    '  }'
    '}'
)


def test_live_ds_text_search_response():
    client, _ = _client(LIVE)
    res = client.search_products(keyword="shirt")
    # code "00" não deve gerar exception.
    assert isinstance(res, AliExpressSearchResult)
    assert res.total == 1
    assert len(res.products) == 1
    prod = res.products[0]
    assert prod.external_id == "100500123"
    assert prod.item_id == "100500123"
    assert prod.sale_price == Decimal("19.90")
    assert prod.original_price == Decimal("29.90")
    assert prod.orders == 10000
    # URLs com "//" viram "https://".
    assert prod.product_url.startswith("https://")
    assert prod.images and prod.images[0].startswith("https://")
