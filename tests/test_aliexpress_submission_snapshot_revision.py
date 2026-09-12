"""FASE 8.0.11Y — revisão auditável/offline do submission snapshot."""

import copy
import uuid
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import AliExpressOrder, AliExpressRemoteOrder, AliExpressSubmissionAttempt
from loja.integrations.aliexpress.submission_snapshot import (
    ADDRESS_VALIDATION_REJECTION_DIAGNOSTIC,
    AliExpressSubmissionSnapshot,
    AliExpressSubmissionSnapshotRevision,
    SubmissionSnapshotError,
    revise_submission_snapshot_shipping,
)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    db.session.remove()


def _key():
    return uuid.uuid4().hex * 2


def _target(db, make_user, *, attempt_count=1, diagnostic=ADDRESS_VALIDATION_REJECTION_DIAGNOSTIC):
    user = make_user()
    source = Order(
        id=uuid.uuid4(), user_id=user.id, checkout_type="product", status="paid",
        subtotal=Decimal("49.90"), shipping=Decimal("0"), discount=Decimal("0"),
        total=Decimal("49.90"), stock_reserved=False,
        customer_name="Cliente Teste", customer_email=user.email,
        customer_phone="55123456789", zip_code="77000000", street="Rua Teste",
        number="10", complement="Apto 12", neighborhood="Centro",
        city="Palmas", state="TO",
    )
    db.session.add(source)
    db.session.flush()
    ali_order = AliExpressOrder(
        id=uuid.uuid4(), order_id=source.id, state="ready",
        attempt_count=attempt_count, attempt_key=_key(),
    )
    db.session.add(ali_order)
    db.session.flush()
    db.session.add(AliExpressSubmissionAttempt(
        id=uuid.uuid4(), aliexpress_order_id=ali_order.id, attempt_key=_key(),
        state="rejected", last_error=diagnostic,
    ))
    old_shipping = {
        "contact_person": "Cliente Teste", "mobile_no": "55123456789",
        "zip": "77000000", "address": "Rua Teste 10 Apto 12 Centro",
        "city": "Palmas", "province": "TO", "country": "BR",
    }
    items = [{
        "order_item_id": str(uuid.uuid4()), "product_id": "100000001",
        "sku_attr": "14:771", "product_count": 1,
        "logistics_service_name": "CAINIAO",
    }]
    snapshot = AliExpressSubmissionSnapshot(
        id=uuid.uuid4(), aliexpress_order_id=ali_order.id,
        shipping_address=copy.deepcopy(old_shipping), items=copy.deepcopy(items),
    )
    db.session.add(snapshot)
    db.session.commit()
    return source, ali_order, snapshot, old_shipping, items


def test_revision_archives_old_snapshot_and_changes_only_shipping(db, make_user):
    source, ali_order, snapshot, old_shipping, items = _target(db, make_user)
    snapshot_id, created_at = snapshot.id, snapshot.created_at
    state, count, attempt_key = ali_order.state, ali_order.attempt_count, ali_order.attempt_key
    source_phone = source.customer_phone
    attempts_before = AliExpressSubmissionAttempt.query.filter_by(aliexpress_order_id=ali_order.id).count()

    result = revise_submission_snapshot_shipping(
        ali_order, corrected_fields={"customer_phone": "+55 (63) 99876-5432"}, session=db.session
    )
    db.session.commit()
    db.session.refresh(snapshot); db.session.refresh(ali_order); db.session.refresh(source)

    assert snapshot.id == snapshot_id and snapshot.created_at == created_at
    assert snapshot.items == items
    assert snapshot.shipping_address == {
        "contact_person": "Cliente Teste", "full_name": "Cliente Teste",
        "mobile_no": "63998765432", "phone_country": "+55", "zip": "77000000",
        "address": "Rua Teste 10 Centro", "address2": "Apto 12",
        "city": "Palmas", "province": "TO", "country": "BR",
    }
    revision = AliExpressSubmissionSnapshotRevision.query.filter_by(aliexpress_order_id=ali_order.id).one()
    assert revision.revision_no == 1 and revision.snapshot_id == snapshot_id
    assert revision.shipping_address == old_shipping and revision.items == items
    assert revision.changed_fields == list(result.changed_fields)
    assert set(revision.changed_fields) == {"address", "address2", "full_name", "mobile_no", "phone_country"}
    assert "63998765432" not in repr(revision.changed_fields) and "+55" not in revision.changed_fields
    assert source.customer_phone == source_phone
    assert (ali_order.state, ali_order.attempt_count, ali_order.attempt_key) == (state, count, attempt_key)
    assert AliExpressSubmissionAttempt.query.filter_by(aliexpress_order_id=ali_order.id).count() == attempts_before
    assert AliExpressRemoteOrder.query.filter_by(aliexpress_order_id=ali_order.id).count() == 0


def test_revision_requires_explicit_correction(db, make_user):
    _, order, _, _, _ = _target(db, make_user)
    with pytest.raises(SubmissionSnapshotError, match="snapshot_revision_corrections_missing"):
        revise_submission_snapshot_shipping(order, corrected_fields={}, session=db.session)


def test_revision_rejects_unknown_correction_field(db, make_user):
    _, order, _, _, _ = _target(db, make_user)
    with pytest.raises(SubmissionSnapshotError, match="snapshot_revision_unknown_field"):
        revise_submission_snapshot_shipping(order, corrected_fields={"cpf": "000"}, session=db.session)


def test_revision_blocks_remote_identity(db, make_user):
    _, order, _, _, _ = _target(db, make_user)
    db.session.add(AliExpressRemoteOrder(
        id=uuid.uuid4(), aliexpress_order_id=order.id,
        remote_order_id=str(uuid.uuid4().int), position=0,
    )); db.session.commit()
    with pytest.raises(SubmissionSnapshotError, match="snapshot_revision_remote_identity_present"):
        revise_submission_snapshot_shipping(order, corrected_fields={"customer_phone": "63998765432"}, session=db.session)


@pytest.mark.parametrize("attempt_state", ["in_flight", "unknown"])
def test_revision_blocks_unresolved_attempt(db, make_user, attempt_state):
    _, order, _, _, _ = _target(db, make_user, attempt_count=2)
    db.session.add(AliExpressSubmissionAttempt(
        id=uuid.uuid4(), aliexpress_order_id=order.id, attempt_key=_key(), state=attempt_state,
        last_error="submission_unknown" if attempt_state == "unknown" else None,
    )); db.session.commit()
    with pytest.raises(SubmissionSnapshotError, match="snapshot_revision_attempt_unresolved"):
        revise_submission_snapshot_shipping(order, corrected_fields={"customer_phone": "63998765432"}, session=db.session)


def test_revision_requires_exact_address_rejection(db, make_user):
    _, order, _, _, _ = _target(db, make_user, diagnostic="placeorder_remote_error|remote_error_code=OTHER")
    with pytest.raises(SubmissionSnapshotError, match="snapshot_revision_requires_address_rejection"):
        revise_submission_snapshot_shipping(order, corrected_fields={"customer_phone": "63998765432"}, session=db.session)


def test_revision_blocks_attempt_ledger_mismatch(db, make_user):
    _, order, _, _, _ = _target(db, make_user, attempt_count=2)
    with pytest.raises(SubmissionSnapshotError, match="snapshot_revision_attempt_ledger_mismatch"):
        revise_submission_snapshot_shipping(order, corrected_fields={"customer_phone": "63998765432"}, session=db.session)


def test_second_revision_archives_previous_active_version(db, make_user):
    source, order, snapshot, _, _ = _target(db, make_user)
    revise_submission_snapshot_shipping(order, corrected_fields={"customer_phone": "63998765432"}, session=db.session)
    db.session.commit()
    first_active = copy.deepcopy(dict(snapshot.shipping_address))
    second = revise_submission_snapshot_shipping(
        order,
        corrected_fields={"customer_phone": "63998765432", "complement": "Bloco B"},
        session=db.session,
    )
    db.session.commit()
    revisions = (AliExpressSubmissionSnapshotRevision.query.filter_by(aliexpress_order_id=order.id)
                 .order_by(AliExpressSubmissionSnapshotRevision.revision_no).all())
    assert [r.revision_no for r in revisions] == [1, 2]
    assert revisions[1].shipping_address == first_active
    assert second.snapshot.shipping_address["address2"] == "Bloco B"
    assert source.complement == "Apto 12"
