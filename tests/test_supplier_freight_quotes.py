"""Testes do armazenamento de cotação de referência (sem rede/compra)."""
from datetime import datetime, timedelta
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

FROZEN_NOW = datetime(2030, 6, 15, 12, 0, 0)


@pytest.fixture(autouse=True)
def reference_quote_table(db):
    db.session.execute(
        text(
            """CREATE TABLE IF NOT EXISTS supplier_reference_freight_quotes (
                supplier_product_id VARCHAR(36) NOT NULL,
                dest_prefix VARCHAR(3) NOT NULL,
                option_key VARCHAR(120) NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL,
                currency VARCHAR(3) NOT NULL,
                cost NUMERIC(12,2) NOT NULL,
                min_days INTEGER,
                max_days INTEGER,
                quoted_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                provider_method VARCHAR(120) NOT NULL,
                evidence_ref_hash VARCHAR(64),
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (supplier_product_id, dest_prefix, option_key, quantity),
                CHECK (length(dest_prefix) = 3),
                CHECK (quantity > 0),
                CHECK (currency = 'BRL'),
                CHECK (cost >= 0),
                CHECK (expires_at > quoted_at)
            )"""
        )
    )
    db.session.commit()
    yield


def _quote(**overrides):
    value = {
        "currency": "BRL",
        "cost": "12.34",
        "quoted_at": FROZEN_NOW,
        "ttl_seconds": 1800,
        "min_days": 8,
        "max_days": 14,
    }
    value.update(overrides)
    return value


def _store(db, **overrides):
    from loja.services.supplier_freight_quotes import store_reference_freight_quote

    values = {
        "supplier_product_id": uuid.uuid4(),
        "dest_cep": "77000000",
        "option": None,
        "qty": 1,
        "quote": _quote(),
        "provider_method": "read-only-test",
        "evidence_ref": "opaque-test-reference",
        "now": FROZEN_NOW,
    }
    values.update(overrides)
    return store_reference_freight_quote(**values)


def test_valid_window_is_stored_and_loaded(db):
    from loja.services.supplier_freight_quotes import find_valid_reference_quotes

    sp_id = uuid.uuid4()
    _store(db, supplier_product_id=sp_id)

    snapshot = find_valid_reference_quotes(
        dest_cep="77000000", qty=1, now=FROZEN_NOW
    )

    assert list(snapshot) == [str(sp_id)]
    assert snapshot[str(sp_id)]["cost"] == Decimal("12.34")
    assert snapshot[str(sp_id)]["ttl_seconds"] == 1800


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_database_rejects_nonpositive_window(db, delta):
    sp_id = str(uuid.uuid4())
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                """INSERT INTO supplier_reference_freight_quotes
                   (supplier_product_id, dest_prefix, option_key, quantity,
                    currency, cost, quoted_at, expires_at, provider_method,
                    updated_at)
                   VALUES (:sp, '770', '', 1, 'BRL', 1.00, :quoted, :expires,
                           'test', :quoted)"""
            ),
            {
                "sp": sp_id,
                "quoted": FROZEN_NOW,
                "expires": FROZEN_NOW + delta,
            },
        )
        db.session.commit()
    db.session.rollback()


def test_service_rejects_zero_ttl_without_writing(db):
    from loja.services.supplier_freight_quotes import ReferenceQuoteUnavailable

    sp_id = uuid.uuid4()
    with pytest.raises(ReferenceQuoteUnavailable) as exc:
        _store(db, supplier_product_id=sp_id, quote=_quote(ttl_seconds=0))

    assert exc.value.code == "invalid_quote"
    count = db.session.execute(
        text(
            "SELECT count(*) FROM supplier_reference_freight_quotes "
            "WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).scalar()
    assert count == 0


def test_expired_quote_is_not_returned(db):
    from loja.services.supplier_freight_quotes import find_valid_reference_quotes

    sp_id = uuid.uuid4()
    _store(db, supplier_product_id=sp_id)

    snapshot = find_valid_reference_quotes(
        dest_cep="77000000",
        qty=1,
        now=FROZEN_NOW + timedelta(minutes=31),
    )

    assert str(sp_id) not in snapshot


def test_upsert_keeps_one_current_row(db):
    sp_id = uuid.uuid4()
    _store(db, supplier_product_id=sp_id, quote=_quote(cost="12.00"))
    _store(
        db,
        supplier_product_id=sp_id,
        quote=_quote(
            cost="15.00",
            quoted_at=FROZEN_NOW + timedelta(minutes=1),
        ),
        now=FROZEN_NOW + timedelta(minutes=1),
    )

    rows = db.session.execute(
        text(
            "SELECT cost FROM supplier_reference_freight_quotes "
            "WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).all()
    assert len(rows) == 1
    assert Decimal(str(rows[0][0])) == Decimal("15.00")


def test_older_quote_never_overwrites_newer(db):
    sp_id = uuid.uuid4()
    _store(
        db,
        supplier_product_id=sp_id,
        quote=_quote(cost="15.00", quoted_at=FROZEN_NOW + timedelta(minutes=1)),
        now=FROZEN_NOW + timedelta(minutes=1),
    )
    _store(db, supplier_product_id=sp_id, quote=_quote(cost="9.00"))

    cost = db.session.execute(
        text(
            "SELECT cost FROM supplier_reference_freight_quotes "
            "WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).scalar()
    assert Decimal(str(cost)) == Decimal("15.00")


def test_provider_is_explicit_and_called_once(db):
    from loja.services.supplier_freight_quotes import request_reference_freight_quote

    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return {
            **_quote(),
            "provider_method": "read-only-test",
            "evidence_ref": "opaque",
        }

    request_reference_freight_quote(
        supplier_product_id=uuid.uuid4(),
        dest_cep="77000000",
        provider=provider,
        now=FROZEN_NOW,
    )

    assert len(calls) == 1
    assert calls[0]["qty"] == 1


def test_invalid_provider_is_sanitized_and_writes_nothing(db):
    from loja.services.supplier_freight_quotes import (
        ReferenceQuoteUnavailable,
        request_reference_freight_quote,
    )

    sp_id = uuid.uuid4()

    def provider(**kwargs):
        raise RuntimeError("token=secret payload=private")

    with pytest.raises(ReferenceQuoteUnavailable) as exc:
        request_reference_freight_quote(
            supplier_product_id=sp_id,
            dest_cep="77000000",
            provider=provider,
            now=FROZEN_NOW,
        )

    assert exc.value.code == "provider_unavailable"
    assert "secret" not in str(exc.value)
    count = db.session.execute(
        text(
            "SELECT count(*) FROM supplier_reference_freight_quotes "
            "WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).scalar()
    assert count == 0


def test_readiness_snapshot_uses_persisted_quote(db, app, monkeypatch):
    from loja.services.sales_readiness import load_fresh_quotes_snapshot

    sp_id = uuid.uuid4()
    _store(db, supplier_product_id=sp_id)
    monkeypatch.setitem(app.config, "SHIPPING_ALIEXPRESS_FRESH_QUOTES", {})

    snapshot = load_fresh_quotes_snapshot(
        dest_cep="77000000", option=None, now=FROZEN_NOW
    )

    assert str(sp_id) in snapshot
