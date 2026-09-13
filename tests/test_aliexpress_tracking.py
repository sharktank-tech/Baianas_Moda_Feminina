import pytest

from loja.integrations.aliexpress.tracking import (
    AliExpressTrackingObservation,
    AliExpressTrackingProvider,
    AliExpressTrackingUnavailable,
    HardOffAliExpressTrackingProvider,
    evaluate_tracking_observations,
    fetch_sanitized_tracking,
)


class FakeTrackingProvider(AliExpressTrackingProvider):
    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def fetch_tracking(self, remote_order_count):
        self.calls.append(remote_order_count)
        return self.observations


def test_complete_tracking_is_sanitized():
    snapshot = evaluate_tracking_observations(
        (AliExpressTrackingObservation("LB123456789BR", "Correios"),),
        expected_count=1,
    )
    assert snapshot.complete is True
    assert snapshot.reason is None
    assert snapshot.observations[0].tracking_code == "LB123456789BR"


def test_tracking_pending_is_not_converted_to_empty_string():
    snapshot = evaluate_tracking_observations(
        (AliExpressTrackingObservation(None, None),),
        expected_count=1,
    )
    assert snapshot.complete is False
    assert snapshot.reason == "tracking_pending"
    assert snapshot.observations[0].tracking_code is None


@pytest.mark.parametrize(
    "observation",
    [
        AliExpressTrackingObservation("code with spaces", "carrier"),
        AliExpressTrackingObservation("ABC12", "carrier com espaço"),
        AliExpressTrackingObservation("ABC12", "x/token=secret"),
        {"tracking_code": "ABC12", "carrier": "carrier"},
    ],
)
def test_untrusted_tracking_is_rejected_fail_closed(observation):
    snapshot = evaluate_tracking_observations((observation,), expected_count=1)
    assert snapshot.complete is False
    assert snapshot.reason == "tracking_invalid"
    assert snapshot.observations == ()


def test_multiple_remote_orders_preserve_cardinality_and_order():
    snapshot = evaluate_tracking_observations(
        (
            AliExpressTrackingObservation("ABC12", "C1"),
            AliExpressTrackingObservation("XYZ99", "C2"),
        ),
        expected_count=2,
    )
    assert snapshot.complete is True
    assert [item.tracking_code for item in snapshot.observations] == [
        "ABC12",
        "XYZ99",
    ]


def test_cardinality_mismatch_fails_closed():
    snapshot = evaluate_tracking_observations(
        (AliExpressTrackingObservation("ABC12", "C1"),),
        expected_count=2,
    )
    assert snapshot.reason == "tracking_cardinality_mismatch"


def test_fake_provider_is_injected_without_network():
    provider = FakeTrackingProvider(
        (AliExpressTrackingObservation("ABC12", "C1"),)
    )
    snapshot = fetch_sanitized_tracking(
        remote_order_count=1, provider=provider
    )
    assert snapshot.complete is True
    assert provider.calls == [1]


def test_default_provider_is_hard_off():
    with pytest.raises(
        AliExpressTrackingUnavailable,
        match=r"^aliexpress_tracking_hard_off$",
    ):
        HardOffAliExpressTrackingProvider().fetch_tracking(1)
