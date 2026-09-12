"""FASE 8.0.12C — CPF congelado segue no DTO /sync, sem reconstrução."""

import json
import uuid

from loja.integrations.aliexpress.submission import (
    AliExpressPlaceOrderItem,
    AliExpressSubmissionRequest,
)
from loja.integrations.aliexpress.sync_placeorder import (
    SYNC_PLACEORDER_BUSINESS_PARAM,
    build_sync_placeorder_params,
)


def _request(*, cpf=None):
    shipping = {
        "contact_person": "Cliente Teste",
        "full_name": "Cliente Teste",
        "mobile_no": "63998765432",
        "phone_country": "+55",
        "zip": "77000000",
        "address": "Rua Teste 10 Centro",
        "city": "Palmas",
        "province": "TO",
        "country": "BR",
    }
    if cpf is not None:
        shipping["cpf"] = cpf

    return AliExpressSubmissionRequest(
        aliexpress_order_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        attempt_key="a" * 64,
        shipping_address=shipping,
        items=(
            AliExpressPlaceOrderItem(
                product_id="100000001",
                sku_attr="14:771",
                product_count=1,
            ),
        ),
    )


def test_sync_dto_forwards_cpf_from_frozen_snapshot():
    params = build_sync_placeorder_params(
        app_key="test-key",
        access_token="test-token",
        request=_request(cpf="52998224725"),
        timestamp="1234567890",
    )
    dto = json.loads(params[SYNC_PLACEORDER_BUSINESS_PARAM])

    assert dto["logistics_address"]["cpf"] == "52998224725"


def test_sync_dto_does_not_invent_cpf_when_snapshot_has_none():
    params = build_sync_placeorder_params(
        app_key="test-key",
        access_token="test-token",
        request=_request(),
        timestamp="1234567890",
    )
    dto = json.loads(params[SYNC_PLACEORDER_BUSINESS_PARAM])

    assert "cpf" not in dto["logistics_address"]
