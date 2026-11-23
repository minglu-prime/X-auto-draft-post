"""Time helpers. Everything is compared and stored as timezone-aware UTC."""

from __future__ import annotations

from datetime import date, datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime. Naive input is assumed to already be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    return to_utc(dt).replace(tzinfo=None)


def parse_iso(value: object) -> datetime | None:
    """Parse ISO-8601 strings (incl. trailing 'Z'), dates and datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return to_utc(datetime.fromisoformat(s))
        except ValueError:
            return None
    return None
