"""Timezone helpers.

SQLite has no timezone type, so every instant is stored as a **naive UTC**
``datetime``. Teacher input is wall-clock time in the school's zone; it is
converted to UTC on the way in and converted back only for display. This
matters for the late policy, which compares the deadline with
``first_submitted_at``: storing local time as if it were UTC would move a
deadline by two or three hours.

Assignment deadlines are entered as a calendar date only. They become the last
minute of that day (23:59) in the school's zone, so "due 1/12" means students
have until the end of 1 December - never a guessed or default hour.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    """Current UTC instant, naive, for storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalise an aware or naive value to naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_local_datetime(raw: str, timezone_name: str) -> datetime | None:
    """Parse a full ``datetime-local`` value in the school's zone as naive UTC.

    Kept for callers that genuinely need an exact time. Returns ``None`` for an
    empty value; raises ``ValueError`` for malformed input so the caller can
    show a validation message.
    """
    text = (raw or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(timezone_name))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_local_due_date(raw: str, timezone_name: str) -> datetime | None:
    """Parse a date-only deadline as **23:59** in the school's zone.

    ``raw`` is a ``<input type="date">`` value (``YYYY-MM-DD``). The returned
    instant is naive UTC, like every stored datetime. A legacy full
    ``datetime-local`` value is still accepted unchanged so older forms and
    API callers keep working.

    Returns ``None`` for an empty value; raises ``ValueError`` for anything
    that is neither a date nor a valid datetime.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        # A crafted or older request may still send a full datetime; honour
        # the exact time it asked for instead of silently discarding it.
        return parse_local_datetime(text, timezone_name)
    deadline = datetime.combine(day, time(23, 59), tzinfo=_zone(timezone_name))
    return deadline.astimezone(timezone.utc).replace(tzinfo=None)


def format_local(
    value: datetime | None,
    timezone_name: str,
    fmt: str = "%d/%m/%Y %H:%M",
) -> str:
    """Format a stored naive-UTC instant in the school's zone."""
    if value is None:
        return ""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(_zone(timezone_name)).strftime(fmt)
