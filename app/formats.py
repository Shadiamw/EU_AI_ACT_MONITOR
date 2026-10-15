"""Identifier and timestamp formats shared by the worker and its endpoints."""

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
