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
    db.session.execute(text("DELETE FROM supplier_reference_freight_quotes"))
    db.session.commit()
    yield


def _quote(**overrides):
    value = {
        "dest_prefix": "770",
        "option": None,
        "qty": 1,
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


@pytest.mark.parametrize("field,value", [
    ("dest_prefix", "771"), ("dest_prefix", "77"),
    ("dest_prefix", "77000000"), ("option", "other-service"),
    ("option", False), ("option", 0),
    ("qty", 2), ("qty", 1.5), ("qty", True),
    ("ttl_seconds", True), ("ttl_seconds", 1.5),
    ("ttl_seconds", 10**100),
])
def test_invalid_response_context_never_overwrites_valid_quote(db, field, value):
    from loja.services.supplier_freight_quotes import ReferenceQuoteUnavailable

    sp_id = uuid.uuid4()
    _store(db, supplier_product_id=sp_id)
    with pytest.raises(ReferenceQuoteUnavailable, match="^invalid_quote$"):
        _store(db, supplier_product_id=sp_id, quote=_quote(**{field: value, "cost": "99.00"}))
    rows = db.session.execute(text(
        "SELECT cost FROM supplier_reference_freight_quotes WHERE supplier_product_id = :sp"
    ), {"sp": str(sp_id)}).all()
    assert len(rows) == 1
    assert Decimal(str(rows[0][0])) == Decimal("12.34")


@pytest.mark.parametrize("field", ["dest_prefix", "option", "qty"])
def test_response_must_explicitly_confirm_scenario(db, field):
    from loja.services.supplier_freight_quotes import ReferenceQuoteUnavailable

    quote = _quote()
    del quote[field]
    sp_id = uuid.uuid4()
    with pytest.raises(ReferenceQuoteUnavailable, match="^invalid_quote$"):
        _store(db, supplier_product_id=sp_id, quote=quote)
    assert db.session.execute(text(
        "SELECT count(*) FROM supplier_reference_freight_quotes WHERE supplier_product_id = :sp"
    ), {"sp": str(sp_id)}).scalar() == 0


@pytest.mark.parametrize("overrides", [
    {"dest_cep": "invalid"}, {"qty": 0}, {"qty": -1},
    {"qty": True}, {"qty": 1.5}, {"qty": "1.5"},
    {"qty": 2147483648}, {"option": 123}, {"option": "x" * 121},
])
def test_invalid_request_never_calls_provider(db, overrides):
    from loja.services.supplier_freight_quotes import (
        ReferenceQuoteUnavailable, request_reference_freight_quote,
    )

    calls = []
    args = dict(supplier_product_id=uuid.uuid4(), dest_cep="77000000",
                provider=lambda **kwargs: calls.append(kwargs), now=FROZEN_NOW)
    args.update(overrides)
    with pytest.raises(ReferenceQuoteUnavailable, match="^invalid_quote$"):
        request_reference_freight_quote(**args)
    assert calls == []


def test_provider_mismatched_context_is_not_persisted(db):
    from loja.services.supplier_freight_quotes import (
        ReferenceQuoteUnavailable, request_reference_freight_quote,
    )

    calls = []
    sp_id = uuid.uuid4()

    def provider(**kwargs):
        calls.append(kwargs)
        return {**_quote(dest_prefix="771"), "provider_method": "read-only-test"}

    with pytest.raises(ReferenceQuoteUnavailable, match="^invalid_quote$"):
        request_reference_freight_quote(supplier_product_id=sp_id,
                                       dest_cep="77000000", provider=provider,
                                       now=FROZEN_NOW)
    assert len(calls) == 1
    assert db.session.execute(text(
        "SELECT count(*) FROM supplier_reference_freight_quotes WHERE supplier_product_id = :sp"
    ), {"sp": str(sp_id)}).scalar() == 0


def test_exact_expiry_rejected_by_store_and_snapshot(db):
    from loja.services.supplier_freight_quotes import (
        ReferenceQuoteUnavailable, find_valid_reference_quotes,
    )

    sp_id = uuid.uuid4()
    _store(db, supplier_product_id=sp_id)
    expiry = FROZEN_NOW + timedelta(seconds=1800)
    assert str(sp_id) in find_valid_reference_quotes(
        dest_cep="77000000", now=expiry - timedelta(microseconds=1))
    assert str(sp_id) not in find_valid_reference_quotes(dest_cep="77000000", now=expiry)
    with pytest.raises(ReferenceQuoteUnavailable, match="^invalid_quote$"):
        _store(db, now=expiry)


def test_snapshot_separates_destination_option_and_quantity(db):
    from loja.services.supplier_freight_quotes import find_valid_reference_quotes

    sp_id = uuid.uuid4()
    for prefix, option, qty, cost in [
        ("770", None, 1, "12.34"), ("771", None, 1, "21.00"),
        ("770", "express", 1, "35.00"), ("770", None, 2, "45.00"),
    ]:
        _store(db, supplier_product_id=sp_id, dest_cep=prefix + "00000",
               option=option, qty=qty,
               quote=_quote(dest_prefix=prefix, option=option, qty=qty, cost=cost))
        result = find_valid_reference_quotes(dest_cep=prefix + "00000",
                                             option=option, qty=qty, now=FROZEN_NOW)
        assert result[str(sp_id)]["cost"] == Decimal(cost)
    assert find_valid_reference_quotes(dest_cep="77200000", now=FROZEN_NOW) == {}


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


def test_admin_quote_post_gate_off_never_calls_provider(
    client, admin_user, login, app, db, monkeypatch
):
    calls = []
    monkeypatch.setitem(app.config, "ALIEXPRESS_REFERENCE_QUOTE_ENABLED", False)
    monkeypatch.setitem(
        app.config,
        "ALIEXPRESS_REFERENCE_QUOTE_PROVIDER",
        lambda **kwargs: calls.append(kwargs),
    )
    login(admin_user.email, "senha123")
    sp_id = uuid.uuid4()

    response = client.post(
        f"/admin/prontidao-vendas/cotar/{sp_id}",
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert calls == []
    assert db.session.execute(
        text(
            "SELECT count(*) FROM supplier_reference_freight_quotes "
            "WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).scalar() == 0


def test_admin_quote_post_without_reviewed_provider_fails_closed(
    client, admin_user, login, app, db, monkeypatch
):
    monkeypatch.setitem(app.config, "ALIEXPRESS_REFERENCE_QUOTE_ENABLED", True)
    monkeypatch.setitem(app.config, "SALES_READINESS_DESTINATION_CEP", "77000000")
    monkeypatch.delitem(
        app.config, "ALIEXPRESS_REFERENCE_QUOTE_PROVIDER", raising=False
    )
    login(admin_user.email, "senha123")
    sp_id = uuid.uuid4()

    response = client.post(
        f"/admin/prontidao-vendas/cotar/{sp_id}",
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert db.session.execute(
        text(
            "SELECT count(*) FROM supplier_reference_freight_quotes "
            "WHERE supplier_product_id = :sp"
        ),
        {"sp": str(sp_id)},
    ).scalar() == 0


def test_admin_quote_post_requires_admin(
    client, regular_user, login, app, monkeypatch
):
    monkeypatch.setitem(app.config, "ALIEXPRESS_REFERENCE_QUOTE_ENABLED", True)
    login(regular_user.email, "senha123")

    response = client.post(
        f"/admin/prontidao-vendas/cotar/{uuid.uuid4()}",
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 403)


def test_admin_quote_post_rejects_unknown_link_before_provider(
    client, admin_user, login, app, monkeypatch
):
    calls = []
    monkeypatch.setitem(app.config, "ALIEXPRESS_REFERENCE_QUOTE_ENABLED", True)
    monkeypatch.setitem(app.config, "SALES_READINESS_DESTINATION_CEP", "77000000")
    monkeypatch.setitem(
        app.config,
        "ALIEXPRESS_REFERENCE_QUOTE_PROVIDER",
        lambda **kwargs: calls.append(kwargs),
    )
    login(admin_user.email, "senha123")

    response = client.post(
        f"/admin/prontidao-vendas/cotar/{uuid.uuid4()}",
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert calls == []


def test_admin_quote_post_without_csrf_is_rejected(
    client, admin_user, login, app, monkeypatch
):
    monkeypatch.setitem(app.config, "ALIEXPRESS_REFERENCE_QUOTE_ENABLED", True)
    login(admin_user.email, "senha123")
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            f"/admin/prontidao-vendas/cotar/{uuid.uuid4()}",
            follow_redirects=False,
        )
        assert response.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
