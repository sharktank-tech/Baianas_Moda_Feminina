"""FASE 9.0.0-G — sessões agregadas por canal, sem identificador pessoal."""

from __future__ import annotations

from datetime import date

from flask import request
from sqlalchemy import func

from loja import db
from loja.modules.models import AcquisitionDailyMetric
from loja.utils import utcnow


_SESSION_MARKER = "acquisition_session_day"
_COUNTED_ENDPOINTS = {
    "main.home",
    "main.produtos",
    "main.produto_individual",
}


def acquisition_channel(acquisition: dict) -> str:
    source = str(acquisition.get("source") or "").strip()
    if source:
        return source[:140]
    referrer = str(acquisition.get("referrer") or "").strip()
    if referrer:
        return f"referrer:{referrer}"[:140]
    return "direto"


def should_count_acquisition_session(request_like=request) -> bool:
    return (
        getattr(request_like, "method", "") == "GET"
        and getattr(request_like, "endpoint", None) in _COUNTED_ENDPOINTS
    )


def record_acquisition_session(acquisition: dict, *, session, day: date | None = None) -> bool:
    """Incrementa uma vez por sessão/dia; persiste apenas dia, canal e contador."""
    day = day or utcnow().date()
    marker = day.isoformat()
    if session.get(_SESSION_MARKER) == marker:
        return False

    channel = acquisition_channel(acquisition)
    values = {"day": day, "channel": channel, "sessions": 1}
    dialect = db.session.get_bind().dialect.name

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(AcquisitionDailyMetric).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "channel"],
            set_={"sessions": AcquisitionDailyMetric.sessions + 1},
        )
        db.session.execute(stmt)
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        stmt = insert(AcquisitionDailyMetric).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "channel"],
            set_={"sessions": AcquisitionDailyMetric.sessions + 1},
        )
        db.session.execute(stmt)
    else:
        row = (
            AcquisitionDailyMetric.query
            .filter_by(day=day, channel=channel)
            .with_for_update()
            .first()
        )
        if row is None:
            db.session.add(AcquisitionDailyMetric(**values))
        else:
            row.sessions += 1

    db.session.commit()
    session[_SESSION_MARKER] = marker
    return True


def session_counts_by_channel() -> dict[str, int]:
    rows = (
        db.session.query(
            AcquisitionDailyMetric.channel,
            func.sum(AcquisitionDailyMetric.sessions),
        )
        .group_by(AcquisitionDailyMetric.channel)
        .all()
    )
    return {channel: int(total or 0) for channel, total in rows}


__all__ = [
    "acquisition_channel",
    "record_acquisition_session",
    "session_counts_by_channel",
    "should_count_acquisition_session",
]
