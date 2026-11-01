"""Fixtures and helpers for the Phase 4 Part 3 tests.

On the part branch these tests call plan and fire_due directly with the arguments set by the
test and a replaced dispositions.summary, and they never insert disposition rows (Addendum rules
3 and 10). Time is the fake clock (it patches app.clock.now); the HTTP test endpoints are never
used, except in test_escalations_integration.py, which says which clock it uses.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import dispositions, escalations
from tests.worker.conftest import add_event, add_system, rows
from tests.worker.conftest import empty_worker_tables as empty_worker_tables
from tests.worker.conftest import fake_clock as fake_clock
from tests.worker.conftest import receiver as receiver

# The anchor of the spec's worked examples (spec 2.5).
ANCHOR = datetime(2026, 9, 27, 10, 12, tzinfo=UTC)
DAY = timedelta(days=1)
TIER_DAYS = {"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "DEATH": 10, "OTHER_SERIOUS": 15}


class Disposer:
    """Stands in for Part 2: writes the event row and calls plan as the disposition transaction
    does, and answers dispositions.summary with the tier in force (Addendum rule 3)."""

    def __init__(self, app_engine: Engine, owner_engine: Engine) -> None:
        self.app_engine = app_engine
        self.owner_engine = owner_engine
        self.tiers: dict[str, str] = {}

    def summary(self, session: Session, event_id: str) -> dict[str, Any]:
        return {
            "disposition": {
                "tier": self.tiers[event_id],
                "rationale": None,
                "reviewer_id": "rev-1",
                "assigned_at": "2026-09-27T10:12:00Z",
            },
            "revisions": [],
            "deadline_utc": None,
        }

    def event(self, event_id: str = "evt-1", **changes: Any) -> str:
        add_event(self.owner_engine, event_id, detected_at=ANCHOR, received_at=ANCHOR, **changes)
        return event_id

    def dispose(self, event_id: str, tier: str, anchor: datetime = ANCHOR) -> None:
        """tier NOT_SERIOUS closes the event and clears the deadline; a serious tier sets
        deadline_utc = anchor + the tier's days, as Part 2's engine does on calendar days."""
        days = TIER_DAYS.get(tier)
        deadline = None if days is None else anchor + days * DAY
        self.tiers[event_id] = tier
        with Session(self.app_engine) as session:
            session.execute(
                text(
                    "UPDATE event SET state = :state, deadline_utc = :deadline"
                    " WHERE event_id = :event_id"
                ),
                {
                    "state": "CLOSED" if deadline is None else "DISPOSITIONED",
                    "deadline": deadline,
                    "event_id": event_id,
                },
            )
            escalations.plan(session, event_id, anchor, deadline)
            session.commit()


@pytest.fixture
def disposer(app_engine: Engine, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Disposer:
    add_system(owner_engine, "sys-42")
    stand_in = Disposer(app_engine, owner_engine)
    monkeypatch.setattr(dispositions, "summary", stand_in.summary)
    return stand_in


def escalation_rows(owner_engine: Engine, event_id: str = "evt-1") -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT pct, status, due_at FROM escalation WHERE event_id = :e ORDER BY pct",
        e=event_id,
    )


def triggered(owner_engine: Engine) -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT actor_id, subject_id, payload FROM ledger_entry"
        " WHERE entry_type = 'REMINDER_TRIGGERED' ORDER BY seq",
    )


def reminders(calls: list[Any]) -> list[Any]:
    """The reminder bodies a receiver got. Every count filters by type (Addendum rule 7)."""
    return [call.body for call in calls if call.body["notification_type"] == "reminder"]
