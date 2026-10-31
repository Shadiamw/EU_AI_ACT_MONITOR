"""Deadline engine (Phase 4 Part 2, spec 2.5 and 6; Addendum rule 1). Pure: no database and no
clock, so every rule can be tested exhaustively. current_calendar reads the environment only.

- compute(anchor, tier, calendar): the absolute Art. 73 deadline of a tier from its anchor (the
  first disposition's assigned_at) under a recorded calendar; None for NOT_SERIOUS (INV-2).
- current_calendar(): the calendar a first disposition records, built from DEADLINE_CALENDAR
  and DEADLINE_HOLIDAYS. Every revision reuses the recorded one, so a configuration change
  never moves an existing event's deadline.
- extends_deadline(prior_tiers, new_tier): Addendum rule 1. Whether the new tier's window is
  longer than the window of the most recent serious tier on the event, which needs a
  rationale (409 DEADLINE_EXTENSION_REQUIRES_RATIONALE without one).
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from app import config

NOT_SERIOUS = "NOT_SERIOUS"
# Days per serious tier (spec 2.5, Pinned). NOT_SERIOUS has no deadline (INV-2).
TIER_DAYS = {"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "DEATH": 10, "OTHER_SERIOUS": 15}
# The four contract tier strings (REQ-08b), in the contract's order.
TIERS = ("DEATH", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS", NOT_SERIOUS)

CALENDAR_DAYS = "calendar_days"
_WORKING_DAYS_PREFIX = "working_days;holidays="


def _working_days_holidays(calendar: str) -> frozenset[date]:
    listed = calendar.removeprefix(_WORKING_DAYS_PREFIX)
    return frozenset(date.fromisoformat(day) for day in listed.split(",") if day)


def compute(anchor: datetime, tier: str, calendar: str) -> datetime | None:
    """The absolute deadline for a tier from its anchor under a recorded calendar (spec 2.5).

    calendar_days adds whole days. working_days steps forward one UTC calendar day at a time
    from the anchor's UTC date, counts a day only if it is Monday to Friday and not a listed
    holiday, stops at the n-th counted day and returns that date at the anchor's UTC time of
    day. The anchor's own date is never counted. The result is in UTC."""
    if tier == NOT_SERIOUS:
        return None
    days = TIER_DAYS[tier]
    if anchor.tzinfo is None:
        raise ValueError("compute needs a timezone-aware anchor")
    anchor = anchor.astimezone(UTC)
    if calendar == CALENDAR_DAYS:
        return anchor + timedelta(days=days)
    if not calendar.startswith(_WORKING_DAYS_PREFIX):
        raise ValueError(f"unknown deadline calendar {calendar!r}")
    holidays = _working_days_holidays(calendar)
    day = anchor.date()
    counted = 0
    while counted < days:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in holidays:
            counted += 1
    return datetime.combine(day, anchor.timetz())


def current_calendar() -> str:
    """The calendar string: calendar_days, or working_days;holidays=YYYY-MM-DD,... (sorted,
    unique; nothing after the = when there are none), from DEADLINE_CALENDAR and
    DEADLINE_HOLIDAYS. Raises
    RuntimeError for a malformed setting."""
    calendar = config.deadline_calendar()
    holidays = config.deadline_holidays()
    if calendar == CALENDAR_DAYS:
        return CALENDAR_DAYS
    return _WORKING_DAYS_PREFIX + ",".join(day.isoformat() for day in holidays)


def most_recent_serious_tier(prior_tiers: Sequence[str]) -> str | None:
    """The tier of the latest disposition row whose tier is not NOT_SERIOUS, read from the rows
    (oldest first), never from a revision's prior_tier (Addendum rule 1)."""
    for tier in reversed(prior_tiers):
        if tier != NOT_SERIOUS:
            return tier
    return None


def extends_deadline(prior_tiers: Sequence[str], new_tier: str) -> bool:
    """Whether putting new_tier in force needs a rationale (Addendum rule 1): it is a serious
    tier whose window is longer than the most recent serious tier's. The anchor and calendar
    are the same for every row of an event, so comparing windows compares deadlines. An event
    with no earlier serious tier (a first disposition, or one whose rows are all NOT_SERIOUS)
    never needs one."""
    if new_tier == NOT_SERIOUS:
        return False
    previous = most_recent_serious_tier(prior_tiers)
    return previous is not None and TIER_DAYS[new_tier] > TIER_DAYS[previous]
