"""The gap sweeper (REQ-02): monitoring-gap events for systems that stopped sending data.

A system is silent since its last sample (monitored_system.last_received_at, set by Part 1's
ingest) or, if it has never sent one, since monitoring started (monitoring_started_at). When it
has been silent for the gap timeout or longer (3 sampling intervals by default, so 900 seconds:
the third missed sample; AC-02, decision 92), the sweeper opens one gap for it: a FLAGGED
monitoring_gap event, GAP_OPENED and a flag_notification for the reviewer. The system has at
most one open gap; its next sample closes it (close_if_open, called inside Part 1's ingest), and
a new silence can then open a new one.

Gap events skip review, deadlines and reports (steps 6 to 10): an outage is not a reportable
incident. All times come from clock.now(), the simulated clock in the test environment.

Lock order (spec, 'Transactions and lock order (Pinned)'): the monitored_system row, then the
gap and event rows, then the ledger lock inside append. Each gap is opened in its own short
transaction, so one failing system does not stop the others and the ledger lock is held only
briefly.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import clock, config, events, ledger, notifications
from app.formats import api_time, new_id, whole_seconds
from app.ledger.canonical import LedgerError

logger = logging.getLogger("worker")

WORKER_ACTOR = "system"  # background work (spec, 'Ledger entry format')


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
                    " WHERE COALESCE(s.last_received_at, s.monitoring_started_at) <= :cutoff"
                    " AND NOT EXISTS (SELECT 1 FROM monitoring_gap g"
                    " WHERE g.system_id = s.system_id AND g.closed_at IS NULL)"
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
            "SELECT last_received_at, monitoring_started_at FROM monitored_system"
            " WHERE system_id = :system_id FOR UPDATE"
        ),
        {"system_id": system_id},
    ).one_or_none()
    if system is None:
        return None
    # Re-check under the row lock: a sample or another sweeper may have got here first.
    now = clock.now()
    silent_since: datetime = system.last_received_at or system.monitoring_started_at
    if now - silent_since < timeout or _open_gap_id(session, system_id) is not None:
        return None

    event_id = new_id("evt")
    events.create_gap_event(session, event_id, system_id, now)
    session.execute(
        text(
            "INSERT INTO monitoring_gap"
            " (event_id, system_id, last_received_at, silent_since, opened_at)"
            " VALUES (:event_id, :system_id, :last_received_at, :silent_since, :now)"
        ),
        {
            "event_id": event_id,
            "system_id": system_id,
            "last_received_at": system.last_received_at,
            "silent_since": silent_since,
            "now": now,
        },
    )
    last_received_at = system.last_received_at
    ledger.append(
        session,
        "GAP_OPENED",
        WORKER_ACTOR,
        event_id,
        {
            "event_id": event_id,
            "system_id": system_id,
            "last_received_at": None if last_received_at is None else api_time(last_received_at),
        },
    )
    notifications.enqueue(session, event_id, WORKER_ACTOR)
    return event_id


def _open_gap_id(session: Session, system_id: str) -> str | None:
    return session.execute(
        text(
            "SELECT event_id FROM monitoring_gap"
            " WHERE system_id = :system_id AND closed_at IS NULL FOR UPDATE"
        ),
        {"system_id": system_id},
    ).scalar_one_or_none()


def close_if_open(session: Session, system_id: str) -> str | None:
    """Close the system's open gap, if any, in the caller's transaction (Part 1's ingest, step
    6). Appends GAP_CLOSED and returns the gap event id, or returns None and writes nothing.

    gap_seconds is the whole time the system was silent: from its last sample (or the start of
    monitoring) to now, rounded down. The caller commits.
    """
    # Lock order: the system row first. Ingest already holds it; locking again is a no-op.
    session.execute(
        text("SELECT 1 FROM monitored_system WHERE system_id = :system_id FOR UPDATE"),
        {"system_id": system_id},
    )
    event_id = _open_gap_id(session, system_id)
    if event_id is None:
        return None
    silent_since = session.execute(
        text("SELECT silent_since FROM monitoring_gap WHERE event_id = :event_id"),
        {"event_id": event_id},
    ).scalar_one()
    now = clock.now()
    gap_seconds = whole_seconds(silent_since, now)
    session.execute(
        text(
            "UPDATE monitoring_gap SET closed_at = :now, gap_seconds = :gap_seconds"
            " WHERE event_id = :event_id"
        ),
        {"now": now, "gap_seconds": gap_seconds, "event_id": event_id},
    )
    events.close_gap_event(session, event_id, now)
    ledger.append(
        session,
        "GAP_CLOSED",
        WORKER_ACTOR,
        event_id,
        {"event_id": event_id, "system_id": system_id, "gap_seconds": gap_seconds},
    )
    return event_id
