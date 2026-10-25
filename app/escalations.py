"""Escalations (Phase 4 Part 3): the reminder plan of a dispositioned event and the firing of
due reminders (Phase 4 spec 3, Addendum rules 3 and 5).

STUB for Phase 4 Part 3: written by the Phase 4 base commit. plan and fire_due have the
behavior of spec section 11 and Part 3 replaces their bodies; the signatures stay. instant is
the real function (Addendum rule 3), because it is pure: Part 3 uses it and does not change it.
"""

from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session

_MICROSECOND = timedelta(microseconds=1)


def instant(anchor: datetime, deadline: datetime, pct: int) -> datetime:
    """The moment the pct reminder is due: anchor + ceil((deadline - anchor) * pct / 100),
    computed in whole microseconds (spec 6)."""
    window = (deadline - anchor) // _MICROSECOND
    return anchor + timedelta(microseconds=-(-window * pct // 100))


def plan(session: Session, event_id: str, anchor: datetime, deadline_utc: datetime | None) -> None:
    """Plan, re-plan or (with deadline_utc None) cancel the event's unsent reminders, in the
    caller's disposition transaction (spec 3.3).

    STUB for Phase 4 Part 3: does nothing."""


def fire_due(engine: Engine) -> list[tuple[str, int]]:
    """Fire every due reminder exactly once, each in its own transaction, and return the
    (event_id, pct) pairs fired (spec 3.4).

    STUB for Phase 4 Part 3: returns []."""
    return []
