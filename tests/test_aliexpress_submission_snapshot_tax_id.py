"""FASE 8.0.12A — testes offline do CPF no submission snapshot."""

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
)
from loja.integrations.aliexpress.submission_snapshot_tax_id import (
    _normalize_br_cpf,
    revise_submission_snapshot_cpf,
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
        customer_phone="63981154698", zip_code="77000000", street="Rua Teste",
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

    shipping = {
        "contact_person": "Cliente Teste",
        "full_name": "Cliente Teste",
        "mobile_no": "63981154698",
        "phone_country": "+55",
        "zip": "77000000",
        "address": "Rua Teste 10 Centro",
        "address2": "Apto 12",
        "city": "Palmas",
        "province": "TO",
        "country": "BR",
    }
    items = [{
        "order_item_id": str(uuid.uuid4()),
        "product_id": "100000001",
        "sku_attr": "14:771",
        "product_count": 1,
    }]
    snapshot = AliExpressSubmissionSnapshot(
        id=uuid.uuid4(), aliexpress_order_id=ali_order.id,
        shipping_address=copy.deepcopy(shipping), items=copy.deepcopy(items),
    )
    db.session.add(snapshot)
    db.session.commit()
    return source, ali_order, snapshot, shipping, items


@pytest.mark.parametrize("raw", ["529.982.247-25", "52998224725"])
def test_normalize_br_cpf_accepts_valid_checksum(raw):
    assert _normalize_br_cpf(raw) == "52998224725"


@pytest.mark.parametrize("raw", ["", "11111111111", "52998224724", "123"])
def test_normalize_br_cpf_rejects_invalid_without_echo(raw):
    with pytest.raises(SubmissionSnapshotError) as exc:
        _normalize_br_cpf(raw)
    assert exc.value.args == ("shipping_cpf_invalid",)
    assert raw not in str(exc.value)


def test_cpf_revision_archives_old_snapshot_and_changes_only_cpf(db, make_user):
    source, ali_order, snapshot, old_shipping, items = _target(db, make_user)
    snapshot_id = snapshot.id
    snapshot_created_at = snapshot.created_at
    state_before = ali_order.state
    attempt_count_before = ali_order.attempt_count
    attempt_key_before = ali_order.attempt_key
    attempts_before = AliExpressSubmissionAttempt.query.filter_by(
        aliexpress_order_id=ali_order.id
    ).count()

    result = revise_submission_snapshot_cpf(
        ali_order,
        cpf="529.982.247-25",
        session=db.session,
    )
    db.session.commit()
    db.session.refresh(snapshot)
    db.session.refresh(ali_order)
    db.session.refresh(source)

    assert snapshot.id == snapshot_id
    assert snapshot.created_at == snapshot_created_at
    assert snapshot.items == items
    assert snapshot.shipping_address == {**old_shipping, "cpf": "52998224725"}
    assert result.changed_fields == ("cpf",)

    revision = AliExpressSubmissionSnapshotRevision.query.filter_by(
        aliexpress_order_id=ali_order.id
    ).one()
    assert revision.revision_no == 1
    assert revision.snapshot_id == snapshot_id
    assert revision.shipping_address == old_shipping
    assert revision.items == items
    assert revision.changed_fields == ["cpf"]
    assert "52998224725" not in repr(revision)

    assert ali_order.state == state_before
    assert ali_order.attempt_count == attempt_count_before
    assert ali_order.attempt_key == attempt_key_before
    assert AliExpressSubmissionAttempt.query.filter_by(
        aliexpress_order_id=ali_order.id
    ).count() == attempts_before
    assert AliExpressRemoteOrder.query.filter_by(
        aliexpress_order_id=ali_order.id
    ).count() == 0


def test_cpf_revision_requires_exact_address_rejection(db, make_user):
    _, ali_order, _, _, _ = _target(
        db,
        make_user,
        diagnostic="placeorder_remote_error|remote_error_code=OTHER",
    )
    with pytest.raises(
        SubmissionSnapshotError,
        match="snapshot_revision_requires_address_rejection",
    ):
        revise_submission_snapshot_cpf(
            ali_order,
            cpf="52998224725",
            session=db.session,
        )


def test_cpf_revision_blocks_remote_identity(db, make_user):
    _, ali_order, _, _, _ = _target(db, make_user)
    db.session.add(AliExpressRemoteOrder(
        id=uuid.uuid4(), aliexpress_order_id=ali_order.id,
        remote_order_id=str(uuid.uuid4().int), position=0,
    ))
    db.session.commit()

    with pytest.raises(
        SubmissionSnapshotError,
        match="snapshot_revision_remote_identity_present",
    ):
        revise_submission_snapshot_cpf(
            ali_order,
            cpf="52998224725",
            session=db.session,
        )


def test_second_revision_archives_current_address_version_before_cpf(db, make_user):
    _, ali_order, snapshot, old_shipping, items = _target(db, make_user)
    db.session.add(AliExpressSubmissionSnapshotRevision(
        id=uuid.uuid4(),
        aliexpress_order_id=ali_order.id,
        snapshot_id=snapshot.id,
        revision_no=1,
        shipping_address={"legacy": "archived"},
        items=copy.deepcopy(items),
        reason_code="shipping_address_correction",
        changed_fields=["mobile_no"],
        snapshot_created_at=snapshot.created_at,
    ))
    db.session.commit()

    revise_submission_snapshot_cpf(
        ali_order,
        cpf="52998224725",
        session=db.session,
    )
    db.session.commit()

    revisions = (
        AliExpressSubmissionSnapshotRevision.query
        .filter_by(aliexpress_order_id=ali_order.id)
        .order_by(AliExpressSubmissionSnapshotRevision.revision_no)
        .all()
    )
    assert [r.revision_no for r in revisions] == [1, 2]
    assert revisions[1].shipping_address == old_shipping
    assert revisions[1].changed_fields == ["cpf"]
