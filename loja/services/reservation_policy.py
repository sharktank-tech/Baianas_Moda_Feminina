"""Política canônica de validade da preferência e reserva de estoque."""

from datetime import datetime, timedelta, timezone

from flask import current_app

DEFAULT_RESERVATION_HOURS = 24
MAX_RESERVATION_HOURS = 24 * 30


def _bounded_hours(value):
    if value is None or str(value).strip() == "":
        return DEFAULT_RESERVATION_HOURS
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("stock_reservation_hours_invalid") from None
    if parsed < 1 or parsed > MAX_RESERVATION_HOURS:
        raise ValueError("stock_reservation_hours_invalid")
    return parsed


def load_reservation_hours(value=None):
    if value is None:
        value = current_app.config.get(
            "STOCK_RESERVATION_HOURS",
            DEFAULT_RESERVATION_HOURS,
        )
    return _bounded_hours(value)


def reservation_deadline(created_at: datetime, *, hours=None) -> datetime:
    if not isinstance(created_at, datetime):
        raise ValueError("stock_reservation_created_at_invalid")
    return created_at + timedelta(hours=load_reservation_hours(hours))


def as_utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ValueError("payment_preference_expiry_invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds")
