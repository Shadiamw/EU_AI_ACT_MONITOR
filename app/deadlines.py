"""Deadline engine (Phase 4 Part 2, spec 2.5). Pure: no database and no clock.

STUB for Phase 4 Part 2: written by the Phase 4 base commit with the behavior of spec section
11. Part 2 replaces compute (the working-days calendar) and current_calendar (DEADLINE_CALENDAR
and DEADLINE_HOLIDAYS); the signatures and TIER_DAYS stay.
"""

from datetime import datetime, timedelta

NOT_SERIOUS = "NOT_SERIOUS"
# Days per serious tier (spec 2.5, Pinned). NOT_SERIOUS has no deadline (INV-2).
TIER_DAYS = {"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "DEATH": 10, "OTHER_SERIOUS": 15}


def compute(anchor: datetime, tier: str, calendar: str) -> datetime | None:
    """The absolute deadline for a tier from its anchor under a recorded calendar.

    STUB for Phase 4 Part 2: calendar days only (anchor + timedelta(days=n)), whatever the
    calendar says."""
    if tier == NOT_SERIOUS:
        return None
    return anchor + timedelta(days=TIER_DAYS[tier])


def current_calendar() -> str:
    """The calendar a first disposition records: "calendar_days" or
    "working_days;holidays=YYYY-MM-DD,..." (spec 2.5).

    STUB for Phase 4 Part 2: always "calendar_days"."""
    return "calendar_days"
