import uuid

import pytest

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressRemoteOrder

from loja.integrations.aliexpress.tracking import (
    AliExpressTrackingObservation,
    AliExpressTrackingProvider,
    AliExpressTrackingUnavailable,
    HardOffAliExpressTrackingProvider,
    evaluate_tracking_observations,
    fetch_sanitized_tracking,
    persist_sanitized_tracking,
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


def _persist_remote_order(*, count=1):
    order = AliExpressOrder(id=uuid.uuid4(), state="submitted")
    db.session.add(order)
    db.session.flush()
    remotes = []
    for position in range(count):
        remote = AliExpressRemoteOrder(
            id=uuid.uuid4(),
            aliexpress_order_id=order.id,
            remote_order_id=f"REMOTE-{uuid.uuid4().hex}",
            position=position,
            remote_status="submitted",
        )
        db.session.add(remote)
        remotes.append(remote)
    db.session.commit()
    return order, remotes


def test_persist_complete_tracking_atomically(db):
    order, remotes = _persist_remote_order(count=2)
    snapshot = evaluate_tracking_observations(
        (
            AliExpressTrackingObservation("ABC12", "C1"),
            AliExpressTrackingObservation("XYZ99", "C2"),
        ),
        expected_count=2,
    )
    result = persist_sanitized_tracking(order, snapshot)
    assert result.accepted is True
    assert result.persisted == 2
    db.session.refresh(remotes[0])
    db.session.refresh(remotes[1])
    assert (remotes[0].tracking_code, remotes[0].carrier) == ("ABC12", "C1")
    assert (remotes[1].tracking_code, remotes[1].carrier) == ("XYZ99", "C2")
    assert remotes[0].tracking_updated_at is not None


def test_tracking_pending_is_persisted_as_noop(db):
    order, remotes = _persist_remote_order()
    snapshot = evaluate_tracking_observations(
        (AliExpressTrackingObservation(None, None),), expected_count=1
    )
    result = persist_sanitized_tracking(order, snapshot)
    assert result.accepted is True
    assert result.persisted == 0
    db.session.refresh(remotes[0])
    assert remotes[0].tracking_code is None
    assert remotes[0].tracking_updated_at is None


def test_tracking_cardinality_mismatch_does_not_partially_write(db):
    order, remotes = _persist_remote_order(count=2)
    snapshot = evaluate_tracking_observations(
        (AliExpressTrackingObservation("ABC12", "C1"),), expected_count=1
    )
    result = persist_sanitized_tracking(order, snapshot)
    assert result.reason == "tracking_cardinality_mismatch"
    assert all(remote.tracking_code is None for remote in remotes)


def test_existing_tracking_is_idempotent(db):
    order, remotes = _persist_remote_order()
    snapshot = evaluate_tracking_observations(
        (AliExpressTrackingObservation("ABC12", "C1"),), expected_count=1
    )
    first = persist_sanitized_tracking(order, snapshot)
    timestamp = remotes[0].tracking_updated_at
    second = persist_sanitized_tracking(order, snapshot)
    assert first.persisted == 1
    assert second.persisted == 0
    assert remotes[0].tracking_updated_at == timestamp


def test_conflicting_tracking_fails_closed_without_overwrite(db):
    order, remotes = _persist_remote_order()
    first = evaluate_tracking_observations(
        (AliExpressTrackingObservation("ABC12", "C1"),), expected_count=1
    )
    persist_sanitized_tracking(order, first)
    conflict = evaluate_tracking_observations(
        (AliExpressTrackingObservation("XYZ99", "C2"),), expected_count=1
    )
    result = persist_sanitized_tracking(order, conflict)
    assert result.reason == "tracking_identity_conflict"
    db.session.refresh(remotes[0])
    assert (remotes[0].tracking_code, remotes[0].carrier) == ("ABC12", "C1")
