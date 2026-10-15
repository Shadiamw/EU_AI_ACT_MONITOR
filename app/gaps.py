"""The gap sweeper (REQ-02): monitoring-gap events for systems that stopped sending data.

A system is silent since its last sample (monitored_system.last_received_at, set at seed time
and by Part 1's ingest on every accepted sample). When it has been silent for the gap timeout or
longer (3 sampling intervals by default, so 900 seconds: the third missed sample; AC-02,
decision 92), the sweeper opens one gap for it: a FLAGGED monitoring_gap event with four
NOT_APPLICABLE harm fields, GAP_OPENED and a flag_notification for the reviewer (spec 3.4). The
system has at most one open gap; its next sample closes it (close_if_open, called inside Part
1's ingest), and a new silence can then open a new one.

Gap events live in Part 1's event and harm_field tables (spec 1.7), written here with the spec's
column names. All times come from clock.now(), the simulated clock in the test environment.

Lock order (spec 7): the monitored_system row, then the event rows, then the ledger lock inside
append. Each gap is opened in its own short transaction, so one failing system does not stop the
others and the ledger lock is held only briefly.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import clock, config, ledger, notifications
from app.formats import api_time, new_id, retain_until, whole_seconds
from app.ledger.canonical import LedgerError

logger = logging.getLogger("worker")

WORKER_ACTOR = "system"  # background work (spec, 'Ledger entry format')
GAP_EVENT_TYPE = "monitoring_gap"

# The four harm fields every event carries (spec 1.5). A gap event has no harm evidence, so all
# four are NOT_APPLICABLE with null evidence and confirmer (spec 3.4).
HARM_FIELDS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)

_OPEN_GAP = (
    "SELECT event_id, received_at FROM event"
    " WHERE system_id = :system_id AND type = 'monitoring_gap' AND state = 'FLAGGED'"
    " FOR UPDATE"
)


def sweep(engine: Engine) -> list[str]:
    """Open a gap for every system silent for at least the timeout. Returns the new gap
    event ids. Errors are logged by type name and rolled back; that system is retried on the
    next sweep."""
    timeout = timedelta(seconds=config.gap_timeout_seconds())
    with Session(engine) as session:
        candidates = list(
            session.execute(
                text(
                    "SELECT s.system_id FROM monitored_system s"
                    " WHERE s.last_received_at <= :cutoff"
                    " AND NOT EXISTS (SELECT 1 FROM event e WHERE e.system_id = s.system_id"
                    " AND e.type = 'monitoring_gap' AND e.state = 'FLAGGED')"
                    " ORDER BY s.system_id"
                ),
                {"cutoff": clock.now() - timeout},
            ).scalars()
        )
    opened = []
    for system_id in candidates:
        try:
            with Session(engine) as session:
                event_id = _open_gap(session, system_id, timeout)
                session.commit()
        except (SQLAlchemyError, LedgerError) as exc:
            logger.warning("gap sweep failed: system_id=%s type=%s", system_id, type(exc).__name__)
            continue
        if event_id is not None:
            opened.append(event_id)
    return opened


def _open_gap(session: Session, system_id: str, timeout: timedelta) -> str | None:
    system = session.execute(
        text(
            "SELECT last_received_at FROM monitored_system WHERE system_id = :system_id FOR UPDATE"
        ),
        {"system_id": system_id},
    ).one_or_none()
    if system is None:
        return None
    # Re-check under the row lock: a sample or another sweeper may have got here first.
    now = clock.now()
    last_received_at: datetime = system.last_received_at
    if now - last_received_at < timeout or _open_gap_row(session, system_id) is not None:
        return None

    event_id = new_id("evt")
    _insert_gap_event(session, event_id, system_id, now)
    ledger.append(
        session,
        "GAP_OPENED",
        WORKER_ACTOR,
        event_id,
        {
            "event_id": event_id,
            "system_id": system_id,
            "last_received_at": api_time(last_received_at),
        },
    )
    notifications.enqueue(session, event_id, WORKER_ACTOR)
    return event_id


def _insert_gap_event(session: Session, event_id: str, system_id: str, now: datetime) -> None:
    """The gap event and its four harm_field rows (spec 3.4 table). Metric, threshold and
    evidence columns stay null; detected_at and received_at are both the detection time."""
    session.execute(
        text(
            "INSERT INTO event (event_id, system_id, type, detected_at, received_at, state,"
            " retain_until) VALUES (:event_id, :system_id, :type, :now, :now, 'FLAGGED',"
            " :retain_until)"
        ),
        {
            "event_id": event_id,
            "system_id": system_id,
            "type": GAP_EVENT_TYPE,
            "now": now,
            "retain_until": retain_until(now),
        },
    )
    session.execute(
        text(
            "INSERT INTO harm_field (event_id, field_key, status)"
            " VALUES (:event_id, :field_key, 'NOT_APPLICABLE')"
        ),
        [{"event_id": event_id, "field_key": key} for key in HARM_FIELDS],
    )


def _open_gap_row(session: Session, system_id: str) -> tuple[str, datetime] | None:
    row = session.execute(text(_OPEN_GAP), {"system_id": system_id}).one_or_none()
    return None if row is None else (row.event_id, row.received_at)


def close_if_open(session: Session, system_id: str) -> str | None:
    """Close the system's open gap, if any, in the caller's transaction (Part 1's ingest, step
    6). Appends GAP_CLOSED and returns the gap event id, or returns None and writes nothing.

    gap_seconds is the outage length from the gap's received_at (its detection) to now,
    rounded down to whole seconds (spec 3.4). The caller commits.
    """
    # Lock order: the system row first. Ingest already holds it; locking again is a no-op.
    session.execute(
        text("SELECT 1 FROM monitored_system WHERE system_id = :system_id FOR UPDATE"),
        {"system_id": system_id},
    )
    gap = _open_gap_row(session, system_id)
    if gap is None:
        return None
    event_id, detected = gap
    gap_seconds = whole_seconds(detected, clock.now())
    session.execute(
        text("UPDATE event SET state = 'CLOSED' WHERE event_id = :event_id"),
        {"event_id": event_id},
    )
    ledger.append(
        session,
        "GAP_CLOSED",
        WORKER_ACTOR,
        event_id,
        {"event_id": event_id, "system_id": system_id, "gap_seconds": gap_seconds},
    )
    return event_id
