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
    _extract_sku_items,
    _normalize_skus,
    build_product_detail_params,
    normalize_product_detail,
)


PRODUCT_JSON = {
    "result": {
        "ae_item_base_info_dto": {
            "product_id": 32839190109,
            "category_id": 123456,
            "subject": "Test product",
            "currency_code": "BRL",
            "detail": "<div>description</div>",
        },
        "ae_item_sku_info_dtos": {
            "ae_item_sku_info_d_t_o": [
                {
                    "id": "sku-1",
                    "sku_price": "39.90",
                    "offer_sale_price": "29.90",
                    "currency_code": "BRL",
                    "sku_available_stock": 12,
                    "ae_sku_property_dtos": {
                        "ae_sku_property_d_t_o": [
                            {"sku_property_name": "Color", "sku_property_value": "Black"}
                        ]
                    },
                }
            ]
        },
        "ae_item_properties": {
            "ae_item_property": [{"attr_name": "Material", "attr_value": "Cotton"}]
        },
        "ae_multimedia_info_dto": {
            "image_urls": "//example.com/a.jpg;//example.com/b.jpg"
        },
    },
    "rsp_code": "200",
    "rsp_msg": "success",
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
    detail = normalize_product_detail(PRODUCT_JSON, method="aliexpress.ds.product.get")

    assert isinstance(detail, AliExpressProductDetail)
    assert detail.product_id == "32839190109"
    assert detail.title == "Test product"
    assert detail.description == "<div>description</div>"
    assert detail.category_id == "123456"
    assert detail.currency == "BRL"
    # URLs relativas (//) normalizadas para https.
    assert detail.images == ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    assert detail.video is None  # só preenche se houver URL real

    assert len(detail.skus) == 1
    sku = detail.skus[0]
    assert isinstance(sku, AliExpressProductSku)
    assert sku.sku_id == "sku-1"
    assert sku.available_stock == 12  # estoque 0 seria válido (first_not_none)
    assert sku.sale_price == Decimal("29.90")
    assert sku.original_price == Decimal("39.90")
    assert sku.currency == "BRL"
    assert sku.attributes == [{"name": "Color", "value": "Black"}]

    # Nível de produto só tem preço/estoque por SKU -> None (sem invenção).
    assert detail.sale_price is None
    assert detail.original_price is None
    assert detail.available_stock is None

    # Atributos gerais.
    assert detail.attributes == [{"name": "Material", "value": "Cotton"}]
    # Result inteiro preservado em raw.
    assert detail.raw.get("ae_item_base_info_dto", {}).get("product_id") == 32839190109
    assert "ae_item_sku_info_dtos" in detail.raw
    assert "ae_item_properties" in detail.raw


# ----- tolerância a simplify=true (SKUs como lista direta) ------------------
def test_extract_sku_items_list_form():
    value = [
        {
            "id": "sku-1",
            "sku_price": "39.90",
            "offer_sale_price": "29.90",
            "sku_available_stock": 12,
        }
    ]
    assert _extract_sku_items(value) is value


def test_extract_sku_items_wrapper_form():
    value = {"ae_item_sku_info_d_t_o": [{"id": "sku-1"}]}
    items = _extract_sku_items(value)
    assert len(items) == 1
    assert items[0]["id"] == "sku-1"


def test_normalize_skus_accepts_list_directly():
    skus = _normalize_skus(
        [
            {
                "id": "sku-1",
                "sku_price": "39.90",
                "offer_sale_price": "29.90",
                "sku_available_stock": 12,
            }
        ]
    )
    assert len(skus) == 1
    assert skus[0].sku_id == "sku-1"
    assert skus[0].available_stock == 12
    assert skus[0].sale_price == Decimal("29.90")
    assert skus[0].original_price == Decimal("39.90")


def test_normalize_product_detail_with_list_skus():
    payload = {
        "result": {
            "ae_item_base_info_dto": {
                "product_id": 32839190109,
                "category_id": 123456,
                "subject": "Test product",
                "currency_code": "BRL",
                "detail": "<div>description</div>",
            },
            "ae_item_sku_info_dtos": [
                {
                    "id": "sku-1",
                    "sku_price": "39.90",
                    "offer_sale_price": "29.90",
                    "sku_available_stock": 12,
                }
            ],
            "ae_item_properties": {
                "ae_item_property": [{"attr_name": "Material", "attr_value": "Cotton"}]
            },
            "ae_multimedia_info_dto": {
                "image_urls": "//example.com/a.jpg;//example.com/b.jpg"
            },
        },
        "rsp_code": "200",
        "rsp_msg": "success",
    }
    detail = normalize_product_detail(payload, method="aliexpress.ds.product.get")
    assert detail.product_id == "32839190109"
    assert len(detail.skus) == 1
    assert detail.skus[0].sku_id == "sku-1"
    assert detail.skus[0].available_stock == 12
    assert detail.skus[0].sale_price == Decimal("29.90")
    assert detail.skus[0].original_price == Decimal("39.90")


# ----- chamada via cliente (transporte falso) -------------------------------
def test_get_product_via_client_builds_request_and_normalizes():
    body = json.dumps(PRODUCT_JSON)
    transport = _FakeTransport(body)
    client = AliExpressClient(
        app_key="k", app_secret="s", access_token="tok", transport=transport
    )
    detail = client.get_product(product_id="32839190109", ship_to_country="BR")

    params = transport.last_params
    assert params["method"] == "aliexpress.ds.product.get"
    assert params["product_id"] == "32839190109"
    assert params["ship_to_country"] == "BR"
    assert params["target_currency"] == "BRL"
    assert params["target_language"] == "pt"
    assert params["remove_personal_benefit"] == "true"
    assert params["session"] == "tok"
    assert params["sign_method"] == "sha256"
    assert "sign" in params
    # O app_secret NUNCA viaja nos parâmetros.
    assert "app_secret" not in params

    assert detail.product_id == "32839190109"
    assert len(detail.skus) == 1
