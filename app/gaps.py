"""The gap sweeper (REQ-02): monitoring-gap events for systems that stopped sending data.

A system is silent since its last sample (monitored_system.last_received_at, set at seed time
and by Part 1's ingest on every accepted sample). When it has been silent for the gap timeout or
longer (its gap_timeout_intervals x sampling_interval_s: 3 x 300 = 900 seconds for sys-42, so
the third missed sample; AC-02, decision 92), the sweeper opens one gap for it: a FLAGGED
monitoring_gap event with four NOT_APPLICABLE harm fields, GAP_OPENED and a flag_notification
for the reviewer (spec 3.4). The
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
from sqlalchemy.orm import Session

from app import clock, ledger, notifications
from app.formats import api_time, new_id, retain_until, whole_seconds

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
    """One sweeper run (spec 3.4): examine every monitored system once and open a gap for each
    one silent for at least its timeout. Returns the new gap event ids.

    Each system is handled in its own transaction. A row another process holds is skipped. An
    error for one system is logged by type name and rolled back, and the others carry on; that
    system is examined again on the next run. Afterwards the real time goes into
    sweeper_heartbeat (spec 3.3).
    """
    with Session(engine) as session:
        system_ids = list(
            session.execute(
                text("SELECT system_id FROM monitored_system ORDER BY system_id")
            ).scalars()
        )
    examined = 0
    opened = []
    for system_id in system_ids:
        try:
            with Session(engine) as session:
                locked, event_id = _sweep_system(session, system_id)
                session.commit()
        except Exception as exc:  # one bad row must never stop the sweep (spec 3.3)
            logger.warning("gap sweep failed: system_id=%s type=%s", system_id, type(exc).__name__)
            continue
        if locked:
            examined += 1
        if event_id is not None:
            opened.append(event_id)
    _write_heartbeat(engine)
    logger.info("gap sweep: systems_examined=%d gaps_opened=%d", examined, len(opened))
    return opened


def _sweep_system(session: Session, system_id: str) -> tuple[bool, str | None]:
    """Lock the system row, skipping it if another process holds it (spec 3.4 step 1). Returns
    whether the row was examined, and the new gap's event id if one was opened."""
    system = session.execute(
        text(
            "SELECT last_received_at, sampling_interval_s, gap_timeout_intervals"
            " FROM monitored_system WHERE system_id = :system_id FOR UPDATE SKIP LOCKED"
        ),
        {"system_id": system_id},
    ).one_or_none()
    if system is None:  # held by another process, or removed since the listing
        return False, None
    # The threshold is per system (spec 3.2): gap_timeout_intervals x sampling_interval_s.
    timeout = timedelta(seconds=system.gap_timeout_intervals * system.sampling_interval_s)
    now = clock.now()
    last_received_at: datetime = system.last_received_at
    if now - last_received_at < timeout or _open_gap_row(session, system_id) is not None:
        return True, None

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
    notifications.enqueue(session, event_id)
    return True, event_id


def _write_heartbeat(engine: Engine) -> None:
    """sweeper_heartbeat.last_ran_at = the real time (spec 3.3), never the simulated clock."""
    try:
        with Session(engine) as session:
            session.execute(
                text(
                    "INSERT INTO sweeper_heartbeat (singleton, last_ran_at) VALUES (true, :t)"
                    " ON CONFLICT (singleton) DO UPDATE SET last_ran_at = EXCLUDED.last_ran_at"
                ),
                {"t": clock.wall_now()},
            )
            session.commit()
    except Exception as exc:
        logger.warning("sweeper heartbeat not written: type=%s", type(exc).__name__)


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
