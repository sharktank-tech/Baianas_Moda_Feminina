import pytest

from loja.integrations.aliexpress.ds_order_status import DS_ORDER_STATUS_METHOD
from loja.integrations.aliexpress.ds_tracking import (
    AliExpressDSTrackingProvider,
    extract_ds_tracking,
)
from loja.integrations.aliexpress.tracking import AliExpressTrackingUnavailable


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def call(self, method, **business):
        self.calls.append((method, business))
        return self.payloads.pop(0)


def _payload(code="LB123456789BR", carrier="Correios"):
    return {
        "result": {
            "logistics_info_list": [
                {
                    "logistics_no": code,
                    "logistics_service": carrier,
                }
            ]
        }
    }


def test_extracts_only_sanitized_logistics_fields():
    observation = extract_ds_tracking(_payload())
    assert observation.tracking_code == "LB123456789BR"
    assert observation.carrier == "Correios"


def test_missing_logistics_is_pending_not_failure():
    observation = extract_ds_tracking({"result": {"order_status": "FINISH"}})
    assert observation.tracking_code is None
    assert observation.carrier is None


def test_nested_result_and_wrapper_shape_are_supported():
    observation = extract_ds_tracking(
        {
            "result": {
                "result": {
                    "logistics_info_list": {
                        "logistics_info": _payload()["result"][
                            "logistics_info_list"
                        ]
                    }
                }
            }
        }
    )
    assert observation.tracking_code == "LB123456789BR"


def test_multiple_tracking_candidates_fail_closed():
    payload = _payload()
    payload["result"]["logistics_info_list"].append(
        {"logistics_no": "AB123", "logistics_service": "C2"}
    )
    with pytest.raises(
        AliExpressTrackingUnavailable,
        match=r"^tracking_payload_ambiguous$",
    ):
        extract_ds_tracking(payload)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"result": {"logistics_info_list": "raw text"}},
        _payload("code with spaces", "Correios"),
        _payload("AB123", "carrier with spaces"),
    ],
)
def test_invalid_or_free_text_payload_fails_closed(payload):
    with pytest.raises(AliExpressTrackingUnavailable):
        extract_ds_tracking(payload)


def test_provider_queries_each_remote_order_in_durable_order():
    client = FakeClient([_payload("ABC12", "C1"), _payload("XYZ99", "C2")])
    provider = AliExpressDSTrackingProvider(
        client=client,
        remote_order_ids=("REMOTE1", "REMOTE2"),
    )
    observations = provider.fetch_tracking(2)
    assert [item.tracking_code for item in observations] == ["ABC12", "XYZ99"]
    assert client.calls == [
        (
            DS_ORDER_STATUS_METHOD,
            {"single_order_query": {"order_id": "REMOTE1"}},
        ),
        (
            DS_ORDER_STATUS_METHOD,
            {"single_order_query": {"order_id": "REMOTE2"}},
        ),
    ]


def test_provider_cardinality_mismatch_makes_zero_calls():
    client = FakeClient([_payload()])
    provider = AliExpressDSTrackingProvider(
        client=client,
        remote_order_ids=("REMOTE1",),
    )
    with pytest.raises(
        AliExpressTrackingUnavailable,
        match=r"^tracking_cardinality_mismatch$",
    ):
        provider.fetch_tracking(2)
    assert client.calls == []


def test_provider_requires_explicit_client():
    with pytest.raises(
        AliExpressTrackingUnavailable,
        match=r"^tracking_client_required$",
    ):
        AliExpressDSTrackingProvider(client=None, remote_order_ids=("REMOTE1",))
