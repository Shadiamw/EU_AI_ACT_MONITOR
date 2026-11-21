"""Part 4 platform operations: test reset and clock advance (spec 4.5, Addendum rule 7)."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import clock, config, escalations, gaps, notifications, observability
from app.formats import TIME_LIMIT
from app.ledger_ops import VERIFICATION_LOCK_KEY
from tools.seed_part1 import seed as install_seed

logger = logging.getLogger("api.platform")

# Session advisory lock key that serialises resets: ASCII "RSET" (Part 3 decision 300),
# distinct from the ledger's "LEDG" and the verification run's "LVRN".
RESET_LOCK_KEY = 1381188948

# Lock order (Part 3 decisions 300 and 302): advisory locks first, then tables.
#   reset():            RSET (session, own connection), then LVRN (in the truncate
#                       transaction, as its first statement), then the TRUNCATE's table locks.
#   run_verification(): LVRN (holder transaction, first statement), then the holder's due
#                       check and insert on ledger_verification_run; the checks connection
#                       reads the tables while the holder waits for it in Python.
# The holder's wait on its checks connection is invisible to PostgreSQL's deadlock detector,
# so no cycle may pass through it: a reset waits for LVRN before it locks any table, so it
# waits for a run in progress, and a run started during a reset waits for the reset's commit
# before its holder touches a table. A reader that locks several tables in one statement (the
# alert step, a request) can still form a cycle with the TRUNCATE; PostgreSQL detects it and
# aborts one side, and when that side is the reset, the truncate transaction is retried.
# A request can also hold table locks on one connection while it waits in Python for another
# (GET /v1/ops/dashboard did until audit 3, R1: it read in its request session, then called
# worker.health_values on a new connection; app/ops.py now reads those values first); with
# the TRUNCATE queued between the two, that cycle is invisible too. So
# once LVRN is held, the truncate transaction waits at most RESET_LOCK_TIMEOUT for a table
# lock (SET LOCAL, this transaction only); a lock timeout gives the waiting request its locks
# back and the transaction is retried. The wait for LVRN itself is not bounded: a run in
# progress always finishes without a lock the reset holds.
RESET_ATTEMPTS = 3
RESET_RETRY_PAUSE_SECONDS = 0.2
RESET_LOCK_TIMEOUT = "2s"
# deadlock_detected and lock_not_available (a lock timeout).
_RETRYABLE_SQLSTATES = frozenset({"40P01", "55P03"})

# Truncate order is free under CASCADE. Owner role only.
RESET_TABLES = (
    "notification_delivery",
    "notification",
    "webhook",
    "harm_field",
    "event",
    "metric_sample",
    "breach_series",
    "ledger_entry",
    "monitored_system",
    "sweeper_heartbeat",
    "api_key",
    "threshold_version",
    "clock_state",
)

# The Phase 4 tables (Phase 4 Addendum 9.1). Migrations 0005 to 0007 create them, so on a
# branch where a migration is still a placeholder its table does not exist yet.
PHASE4_TABLES = ("harm_context", "disposition", "escalation")
# The Phase 5 tables (Phase 5 spec 11.1 step 2); each part appends the tables its migration
# creates. They go through the same to_regclass check, for the same reason.
PHASE5_TABLES = (
    "report",
    "report_submission",
    "record_key",
    "ledger_verification_run",
    "platform_counter",
)


def with_phase4_tables(conn: Connection, tables: Sequence[str] = ()) -> tuple[str, ...]:
    """`tables` followed by each Phase 4 table that exists (to_regclass). Every truncate list
    (RESET_TABLES and the test fixtures' own lists) goes through this, so a table that
    references event or monitored_system is emptied with them."""
    exists = text("SELECT to_regclass(:table) IS NOT NULL")
    existing = tuple(
        table
        for table in (*PHASE4_TABLES, *PHASE5_TABLES)
        if conn.execute(exists, {"table": table}).scalar_one()
    )
    return (*tables, *existing)


def _admin_engine() -> Engine:
    return create_engine(config.database_admin_url(), poolclass=NullPool)


def require_test_environment() -> None:
    if not config.is_test_environment():
        raise RuntimeError("test-only endpoints require ENVIRONMENT=test")


def set_offset_seconds(seconds: int) -> int:
    """Write the single clock_state row. Returns the stored offset."""
    engine = _admin_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM clock_state"))
            conn.execute(
                text("INSERT INTO clock_state (singleton, offset_seconds) VALUES (true, :seconds)"),
                {"seconds": seconds},
            )
    finally:
        engine.dispose()
    return seconds


def reset() -> None:
    """POST /v1/test/reset: empty the tables, zero the clock, reinstall the seed fixtures."""
    require_test_environment()
    engine = _admin_engine()
    try:
        with engine.connect() as lock:
            # Resets run one after another (Part 3 decision 300): a session advisory lock on its
            # own connection is held over the truncate, the seed and the clock step, and released
            # explicitly, even when the reset raises. No table lock is taken.
            lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": RESET_LOCK_KEY})
            lock.commit()
            try:
                _truncate(engine)
                with Session(engine) as session:
                    install_seed(session)
                    session.commit()
                set_offset_seconds(0)
                observability.reset_counters()
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RESET_LOCK_KEY})
                lock.commit()
    finally:
        engine.dispose()


def _truncate(engine: Engine) -> None:
    """The reset's one transaction: LVRN, then a bounded lock wait, read reset_count,
    TRUNCATE, write count + 1 (decision 300). Retried up to RESET_ATTEMPTS times in all when
    PostgreSQL aborts it with a detected deadlock or a lock timeout; each attempt rolls back
    whole, so reset_count rises by exactly one. After the last attempt the error is logged and
    raised."""
    for attempt in range(1, RESET_ATTEMPTS + 1):
        try:
            with engine.begin() as conn:
                # First statement, before any table lock: wait for a verification run in
                # progress, and make a new run wait for this commit (lock order above).
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": VERIFICATION_LOCK_KEY}
                )
                # From here on a table lock is waited for at most RESET_LOCK_TIMEOUT (55P03).
                conn.execute(text(f"SET LOCAL lock_timeout = '{RESET_LOCK_TIMEOUT}'"))
                # platform_counter is in the truncate list, so reset_count is read first and
                # written back as count + 1 in the same transaction (decision 300).
                count = conn.execute(text("SELECT reset_count FROM platform_counter")).scalar()
                tables = with_phase4_tables(conn, RESET_TABLES)
                conn.execute(text(f"TRUNCATE {', '.join(tables)}"))
                conn.execute(
                    text("INSERT INTO platform_counter (reset_count) VALUES (:count)"),
                    {"count": (count or 0) + 1},
                )
            return
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate not in _RETRYABLE_SQLSTATES:
                raise
            if attempt == RESET_ATTEMPTS:
                logger.error(
                    "reset failed: attempts=%d sqlstate=%s type=%s",
                    attempt,
                    sqlstate,
                    type(exc.orig).__name__,
                )
                raise
            logger.warning("reset retried: attempt=%d sqlstate=%s", attempt, sqlstate)
            time.sleep(RESET_RETRY_PAUSE_SECONDS * attempt)


# clock_state.offset_seconds is a BIGINT (migration 0001).
MAX_OFFSET_SECONDS = 2**63 - 1


def _check_representable(offset: int) -> None:
    """ValueError, before anything is written, if `offset` overflows the stored BIGINT or puts
    the simulated time at or after 9999-07-01T00:00:00Z (decision 226, bound moved by D10)."""
    if offset > MAX_OFFSET_SECONDS:
        raise ValueError("offset overflows the stored offset")
    try:
        simulated = clock.wall_now() + timedelta(seconds=offset)
    except OverflowError:
        raise ValueError("simulated time past the year 9999") from None
    if simulated >= TIME_LIMIT:
        raise ValueError("simulated time at or after 9999-07-01")


def advance_clock(seconds: int) -> dict[str, object]:
    """POST /v1/test/clock/advance: add to the offset, then sweep, fire due reminders and
    deliver once each, in that order (Phase 4 spec 4.5). Zero seconds is allowed (LIM-03)."""
    require_test_environment()
    if seconds < 0:
        raise ValueError("seconds must be non-negative")
    new_offset = clock.offset_seconds() + seconds
    _check_representable(new_offset)
    set_offset_seconds(new_offset)
    from app.db import get_engine

    engine = get_engine()
    opened = gaps.sweep(engine)
    fired = escalations.fire_due(engine)
    delivered = notifications.deliver_due(engine)
    return {
        "seconds_advanced": seconds,
        "offset_seconds": new_offset,
        "gaps_opened": opened,
        "reminders_fired": fired,
        "deliveries": delivered,
    }
