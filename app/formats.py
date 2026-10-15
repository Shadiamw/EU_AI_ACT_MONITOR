"""Identifier and timestamp formats shared by the worker and its endpoints."""

import calendar
import uuid
from datetime import UTC, datetime, timedelta


def new_id(prefix: str) -> str:
    """A new opaque id such as `evt-<32 hex digits>`."""
    return f"{prefix}-{uuid.uuid4().hex}"


def api_time(moment: datetime) -> str:
    """ISO-8601 UTC as the API writes it: `YYYY-MM-DDTHH:MM:SSZ`, with fractional seconds
    only when present (spec, 'Shared rules (Pinned)'). A naive datetime is a caller bug."""
    if moment.tzinfo is None:
        raise ValueError("api_time needs a timezone-aware datetime")
    moment = moment.astimezone(UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}".rstrip("0")
    return text + "Z"


def whole_seconds(start: datetime, end: datetime) -> int:
    """Whole seconds from start to end, rounded down, never negative."""
    return max(0, (end - start) // timedelta(seconds=1))


RETENTION_MONTHS = 6
RETENTION_MIN_DAYS = 183


def add_calendar_months(moment: datetime, months: int) -> datetime:
    """The same day and time `months` calendar months later. A day the target month does not
    have becomes that month's last day (31 August + 6 months = 28 or 29 February)."""
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def retain_until(detected_at: datetime) -> datetime:
    """Minimum retention for an event (REQ-05; spec 1.5 and 3.4): detected_at plus six calendar
    months, and at least 183 days, so the later of the two."""
    return max(
        add_calendar_months(detected_at, RETENTION_MONTHS),
        detected_at + timedelta(days=RETENTION_MIN_DAYS),
    )
