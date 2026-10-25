"""Dispositions (Phase 4 Part 2): the disposition and revision history of an event (Phase 4
spec 2).

STUB for Phase 4 Part 2: written by the Phase 4 base commit with the behavior of spec section
11. Part 2 replaces summary's body and adds the disposition handler; summary's signature and
the shape of its result stay.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.formats import api_time


def summary(session: Session, event_id: str) -> dict[str, Any]:
    """{disposition, revisions, deadline_utc} for an event, in the contract's Event form (times
    as API strings), as events.detail puts them in the Event object. Part 3 reads it when it
    fires a reminder (Addendum rule 3).

    STUB for Phase 4 Part 2: no disposition, no revisions, and deadline_utc as stored on the
    event (null for an unknown event)."""
    deadline = session.execute(
        text("SELECT deadline_utc FROM event WHERE event_id = :event_id"),
        {"event_id": event_id},
    ).scalar_one_or_none()
    return {
        "disposition": None,
        "revisions": [],
        "deadline_utc": None if deadline is None else api_time(deadline),
    }
