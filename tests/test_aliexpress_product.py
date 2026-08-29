"""Testes offline do detalhe de produto AliExpress (Fase 3, sem rede)."""

from __future__ import annotations

import json
from decimal import Decimal

from loja.integrations.aliexpress import (
    AliExpressClient,
    AliExpressProductDetail,
    AliExpressProductSku,
)
from loja.integrations.aliexpress.product import (
    build_product_detail_params,
    normalize_product_detail,
)


PRODUCT_JSON = {
    "aliexpress_ds_product_get_response": {
        "code": "00",
        "data": {
            "productId": "100500123",
            "title": "Test product",
            "description": "Nice product",
            "categoryId": "123",
            "images": ["https://x/a.jpg", "//x/b.jpg"],
            "video": "//x/v.mp4",
            "sku": [
                {
                    "skuId": "1",
                    "attributes": [{"name": "Color", "value": "Red"}],
                    "availableStock": 10,
                    "salePrice": "9.90",
                    "originalPrice": "19.90",
                    "currency": "BRL",
                }
            ],
            "salePrice": "9.90",
            "originalPrice": "19.90",
            "currency": "BRL",
            "availableStock": 10,
            "attributes": [{"name": "Brand", "value": "X"}],
            "taxes": {"importTax": "1.50"},
        }
    }
}


class _FakeTransport:
    def __init__(self, body: str):
        self.body = body
        self.last_params: dict = {}

    def post(self, params: dict) -> str:
        self.last_params = dict(params)
        return self.body


# ----- parâmetros ----------------------------------------------------------
def test_build_product_detail_params_defaults():
    params = build_product_detail_params(
        product_id="100500123",
        ship_to_country="BR",
        target_currency="BRL",
        target_language="pt",
        remove_personal_benefit=True,
        biz_model=None,
        province=None,
        city=None,
    )
    assert params["product_id"] == "100500123"
    assert params["ship_to_country"] == "BR"
    assert params["target_currency"] == "BRL"
    assert params["target_language"] == "pt"
    assert params["remove_personal_benefit"] == "true"
    # Opcionais ausentes não devem aparecer.
    assert "biz_model" not in params
    assert "province" not in params
    assert "city" not in params


def test_build_product_detail_params_flags_and_optionals():
    params = build_product_detail_params(
        product_id="1",
        ship_to_country="US",
        target_currency="USD",
        target_language="en",
        remove_personal_benefit=False,
        biz_model="AE",
        province="CA",
        city="LA",
    )
    assert params["remove_personal_benefit"] == "false"
    assert params["biz_model"] == "AE"
    assert params["province"] == "CA"
    assert params["city"] == "LA"


# ----- normalização ---------------------------------------------------------
def test_normalize_product_detail_basic():
    payload = PRODUCT_JSON["aliexpress_ds_product_get_response"]
    detail = normalize_product_detail(payload, method="aliexpress.ds.product.get")

    assert isinstance(detail, AliExpressProductDetail)
    assert detail.product_id == "100500123"
    assert detail.title == "Test product"
    assert detail.description == "Nice product"
    assert detail.category_id == "123"
    # URLs relativas (//) normalizadas.
    assert detail.images[0] == "https://x/a.jpg"
    assert detail.images[1] == "https://x/b.jpg"
    assert detail.video == "https://x/v.mp4"

    assert len(detail.skus) == 1
    sku = detail.skus[0]
    assert isinstance(sku, AliExpressProductSku)
    assert sku.sku_id == "1"
    assert sku.available_stock == 10
    assert sku.sale_price == Decimal("9.90")
    assert sku.original_price == Decimal("19.90")
    assert sku.currency == "BRL"
    assert sku.attributes == [{"name": "Color", "value": "Red"}]

    assert detail.available_stock == 10
    assert detail.sale_price == Decimal("9.90")
    assert detail.original_price == Decimal("19.90")
    assert detail.currency == "BRL"
    # Impostos preservados sem inventar.
    assert detail.taxes == {"importTax": "1.50"}
    assert detail.attributes == [{"name": "Brand", "value": "X"}]
    # Campos desconhecidos preservados em raw.
    assert detail.raw.get("productId") == "100500123"


# ----- chamada via cliente (transporte falso) -------------------------------
def test_get_product_via_client_builds_request_and_normalizes():
    body = json.dumps(PRODUCT_JSON)
    transport = _FakeTransport(body)
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    detail = client.get_product(product_id="100500123", ship_to_country="BR")

    params = transport.last_params
    assert params["method"] == "aliexpress.ds.product.get"
    assert params["product_id"] == "100500123"
    assert params["ship_to_country"] == "BR"
    assert params["target_currency"] == "BRL"
    assert params["target_language"] == "pt"
    assert params["remove_personal_benefit"] == "true"
    assert params["session"] == "tok"
    assert params["sign_method"] == "sha256"
    assert "sign" in params
    # O app_secret NUNCA viaja nos parâmetros.
    assert "app_secret" not in params

    assert detail.product_id == "100500123"
    assert len(detail.skus) == 1
