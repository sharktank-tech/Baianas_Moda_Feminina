"""FASE 9.0.0-G — sessões agregadas sem identificadores pessoais."""

from datetime import date
from types import SimpleNamespace

import pytest

from loja.modules.models import AcquisitionDailyMetric
from loja.services.acquisition_sessions import (
    acquisition_channel,
    first_session_day,
    record_acquisition_session,
    session_counts_by_channel,
    should_count_acquisition_session,
)


DAY = date(2026, 9, 14)


@pytest.fixture(autouse=True)
def _clean_acquisition_metrics(db):
    AcquisitionDailyMetric.query.delete()
    db.session.commit()
    yield
    AcquisitionDailyMetric.query.delete()
    db.session.commit()


def test_channel_prefers_source_then_referrer_then_direct():
    assert acquisition_channel({"source": "instagram", "referrer": "x"}) == "instagram"
    assert acquisition_channel({"source": None, "referrer": "google.com"}) == "referrer:google.com"
    assert acquisition_channel({}) == "direto"


def test_counts_once_per_session_per_day(app, db):
    browser_session = {}
    with app.test_request_context("/"):
        assert record_acquisition_session(
            {"source": "instagram"},
            session=browser_session,
            day=DAY,
        ) is True
        assert record_acquisition_session(
            {"source": "instagram"},
            session=browser_session,
            day=DAY,
        ) is False

    row = AcquisitionDailyMetric.query.one()
    assert row.day == DAY
    assert row.channel == "instagram"
    assert row.sessions == 1


def test_concurrent_style_sessions_increment_same_aggregate(app, db):
    with app.test_request_context("/"):
        record_acquisition_session({"source": "google"}, session={}, day=DAY)
        record_acquisition_session({"source": "google"}, session={}, day=DAY)

    assert session_counts_by_channel() == {"google": 2}
    assert first_session_day() == DAY


def test_different_channels_remain_separate(app, db):
    with app.test_request_context("/"):
        record_acquisition_session({"source": "instagram"}, session={}, day=DAY)
        record_acquisition_session({"source": "google"}, session={}, day=DAY)

    assert session_counts_by_channel() == {
        "google": 1,
        "instagram": 1,
    }


def test_only_public_catalog_gets_are_counted():
    assert should_count_acquisition_session(
        SimpleNamespace(method="GET", endpoint="main.home")
    )
    assert should_count_acquisition_session(
        SimpleNamespace(method="GET", endpoint="main.produto_individual")
    )
    assert not should_count_acquisition_session(
        SimpleNamespace(method="POST", endpoint="main.home")
    )
    assert not should_count_acquisition_session(
        SimpleNamespace(method="GET", endpoint="admin.dashboard")
    )
    assert not should_count_acquisition_session(
        SimpleNamespace(method="GET", endpoint="static")
    )


def test_aggregate_table_contains_no_personal_identifier_columns():
    columns = set(AcquisitionDailyMetric.__table__.columns.keys())
    assert columns == {"id", "day", "channel", "sessions"}
