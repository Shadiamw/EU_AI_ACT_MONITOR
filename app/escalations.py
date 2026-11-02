"""Escalations (Phase 4 Part 3, REQ-11): the reminder plan of a dispositioned event and the
firing of due reminders (Phase 4 spec 3, Addendum rules 3 and 5).

plan runs inside Part 2's disposition transaction, with the event row already locked. fire_due
runs on every worker iteration and every clock advance, so two callers can overlap.

- Exactly once (creation): the primary key (event_id, pct) and the PENDING to SENT change under
  the row lock, in the same transaction as the notification row. A rolled-back attempt leaves
  the row PENDING for the next pass.
- Never early: the due check is repeated under the lock against clock.now(), because a revision
  can move the row after the listing.
- Never missed: every PENDING row with a past due_at is picked up by the next pass.

Lock order (spec 7): the event row, the escalation rows, the notification rows, then the ledger
lock inside append. No network call happens here: delivery is the Phase 3 outbox.

dispositions is imported inside the function that calls it (Addendum 9.6): dispositions imports
this module the same way, so no import cycle exists at load time.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, ledger, notifications
from app.formats import api_time, new_id, whole_seconds

logger = logging.getLogger("worker")

BATCH_SIZE = 100  # spec 3.4 step 1
OVERDUE_AFTER_SECONDS = 60  # spec 3.6: the health value's grace period
WORKER_ACTOR = "system"  # everything the scheduler does (spec 3.2; Addendum rule 4)

_MICROSECOND = timedelta(microseconds=1)
_late_reminders = 0


def late_reminder_count() -> int:
    """Reminders fired more than NOTIFY_MAX_DELAY_SECONDS after their instant, by this process
    (spec 3.8; Part 3 decision 202)."""
    return _late_reminders


def instant(anchor: datetime, deadline: datetime, pct: int) -> datetime:
    """The moment the pct reminder is due: anchor + ceil((deadline - anchor) * pct / 100),
    computed in whole microseconds (spec 6)."""
    window = (deadline - anchor) // _MICROSECOND
    return anchor + timedelta(microseconds=-(-window * pct // 100))


def plan(session: Session, event_id: str, anchor: datetime, deadline_utc: datetime | None) -> None:
    """Plan, re-plan or (with deadline_utc None) cancel the event's unsent reminders, in the
    caller's disposition transaction (spec 3.3, Addendum rule 5). Never commits.

    The first plan is the one that finds no rows: it inserts one PENDING row per configured
    percentage, and that set is the event's for good. A later plan moves PENDING rows and returns
    CANCELLED rows to PENDING at the new instants. A SENT row is never touched."""
    now = clock.now()
    if deadline_utc is None:
        session.execute(
            text(
                "UPDATE escalation SET status = 'CANCELLED', cancelled_at = :now, updated_at = :now"
                " WHERE event_id = :event_id AND status = 'PENDING'"
            ),
            {"event_id": event_id, "now": now},
        )
        return
    rows = session.execute(
        text(
            "SELECT pct, status FROM escalation WHERE event_id = :event_id ORDER BY pct FOR UPDATE"
        ),
        {"event_id": event_id},
    ).all()
    if not rows:
        session.execute(
            text(
                "INSERT INTO escalation (event_id, pct, status, due_at, anchor_at, updated_at)"
                " VALUES (:event_id, :pct, 'PENDING', :due_at, :anchor_at, :now)"
            ),
            [
                {
                    "event_id": event_id,
                    "pct": pct,
                    "due_at": instant(anchor, deadline_utc, pct),
                    "anchor_at": anchor,
                    "now": now,
                }
                for pct in config.escalation_percentages()
            ],
        )
        return
    for row in rows:
        if row.status == "SENT":
            continue
        session.execute(
            text(
                "UPDATE escalation SET status = 'PENDING', due_at = :due_at,"
                " cancelled_at = NULL, updated_at = :now"
                " WHERE event_id = :event_id AND pct = :pct"
            ),
            {
                "event_id": event_id,
                "pct": row.pct,
                "due_at": instant(anchor, deadline_utc, row.pct),
                "now": now,
            },
        )


@dataclass(frozen=True)
class _Fired:
    due_at: datetime
    sent_at: datetime


def fire_due(engine: Engine) -> list[tuple[str, int]]:
    """Fire every due reminder exactly once, each in its own transaction, and return the
    (event_id, pct) pairs fired, in the order they fired (spec 3.4).

    Each pass walks the due rows in (due_at, event_id, pct) order, one batch after another, so
    one large clock jump fires every due percentage in ascending order. A row that was attempted
    is not listed again in the same call: one that fails or is held by another caller would
    otherwise keep the walk from ending (Part 3 decision 200). An error on one row is logged by
    type name and rolled back, and the next pass tries that row again."""
    fired: list[tuple[str, int]] = []
    due = skipped = failed = 0
    after: tuple[datetime, str, int] | None = None
    while True:
        batch = _list_due(engine, after)
        if not batch:
            break
        due += len(batch)
        for due_at, event_id, pct in batch:
            after = (due_at, event_id, pct)
            try:
                outcome = _fire_one(engine, event_id, pct)
            except Exception as exc:  # one bad row must never stop the pass (spec 3.3)
                failed += 1
                logger.warning(
                    "reminder not fired: event_id=%s pct=%d type=%s",
                    event_id,
                    pct,
                    type(exc).__name__,
                )
                continue
            if outcome is None:
                skipped += 1
                continue
            fired.append((event_id, pct))
            _log_fired(event_id, pct, outcome)
    logger.log(
        logging.INFO if due else logging.DEBUG,
        "escalation pass: rows_due=%d fired=%d skipped=%d failed=%d",
        due,
        len(fired),
        skipped,
        failed,
    )
    return fired


def _list_due(
    engine: Engine, after: tuple[datetime, str, int] | None
) -> list[tuple[datetime, str, int]]:
    """Spec 3.4 step 1, without locks. `after` is the last row attempted in this call."""
    params: dict[str, object] = {"now": clock.now(), "limit": BATCH_SIZE}
    resume = ""
    if after is not None:
        resume = " AND (due_at, event_id, pct) > (:due_at, :event_id, :pct)"
        params.update({"due_at": after[0], "event_id": after[1], "pct": after[2]})
    with Session(engine) as session:
        rows = session.execute(
            text(
                "SELECT due_at, event_id, pct FROM escalation"
                f" WHERE status = 'PENDING' AND due_at <= :now{resume}"
                " ORDER BY due_at, event_id, pct LIMIT :limit"
            ),
            params,
        ).all()
    return [(row.due_at, row.event_id, row.pct) for row in rows]


def _fire_one(engine: Engine, event_id: str, pct: int) -> _Fired | None:
    """Spec 3.4 steps 2 to 6, in one transaction. Returns None when the row was skipped."""
    from app import dispositions

    with Session(engine) as session:
        event = session.execute(
            text("SELECT state, deadline_utc FROM event WHERE event_id = :event_id FOR UPDATE"),
            {"event_id": event_id},
        ).one_or_none()
        if event is None:  # emptied by a test reset since the listing
            return None
        row = session.execute(
            text(
                "SELECT status, due_at, anchor_at FROM escalation"
                " WHERE event_id = :event_id AND pct = :pct FOR UPDATE SKIP LOCKED"
            ),
            {"event_id": event_id, "pct": pct},
        ).one_or_none()
        now = clock.now()
        if row is None or row.status != "PENDING" or row.due_at > now:
            return None
        keys = {"event_id": event_id, "pct": pct, "now": now}
        if event.state != "DISPOSITIONED":  # cannot happen with correct planning (spec 3.4 step 3)
            session.execute(
                text(
                    "UPDATE escalation SET status = 'CANCELLED', cancelled_at = :now,"
                    " updated_at = :now WHERE event_id = :event_id AND pct = :pct"
                ),
                keys,
            )
            session.commit()
            logger.warning(
                "reminder cancelled, event not dispositioned: event_id=%s pct=%d state=%s",
                event_id,
                pct,
                event.state,
            )
            return None

        # The decision in force (Addendum rule 3). assigned_at equals the row's anchor_at.
        disposition = dispositions.summary(session, event_id)["disposition"]
        tier: str = disposition["tier"]
        notification_id = new_id("ntf")
        session.execute(
            text(
                "UPDATE escalation SET status = 'SENT', sent_at = :now, updated_at = :now,"
                " notification_id = :notification_id, tier = :tier, deadline_utc = :deadline_utc"
                " WHERE event_id = :event_id AND pct = :pct"
            ),
            {
                **keys,
                "notification_id": notification_id,
                "tier": tier,
                "deadline_utc": event.deadline_utc,
            },
        )
        notifications.enqueue(
            session,
            event_id,
            notifications.REMINDER,
            escalation_pct=pct,
            notification_id=notification_id,
        )
        ledger.append(
            session,
            "REMINDER_TRIGGERED",
            WORKER_ACTOR,
            event_id,
            {
                "event_id": event_id,
                "escalation_pct": pct,
                "due_at": api_time(row.due_at),
                "tier": tier,
                "anchor_at": api_time(row.anchor_at),
                "deadline_utc": api_time(event.deadline_utc),
                "notification_id": notification_id,
            },
        )
        session.commit()
        return _Fired(due_at=row.due_at, sent_at=now)


def _log_fired(event_id: str, pct: int, fired: _Fired) -> None:
    """Spec 3.8: every reminder, and a warning (counted) when it fired late."""
    global _late_reminders
    late_seconds = whole_seconds(fired.due_at, fired.sent_at)
    logger.info(
        "reminder fired: event_id=%s pct=%d due_at=%s late_seconds=%d",
        event_id,
        pct,
        api_time(fired.due_at),
        late_seconds,
    )
    bound = config.notify_max_delay_seconds()
    if late_seconds > bound:
        _late_reminders += 1
        logger.warning(
            "late reminder: event_id=%s pct=%d late_seconds=%d bound_seconds=%d",
            event_id,
            pct,
            late_seconds,
            bound,
        )
