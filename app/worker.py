"""The worker process (Part 3): the gap sweeper every minute, and the delivery pass.

Run it with `python -m app.worker`. It connects as the application role (DATABASE_URL), like
the API. Wall-clock time only paces the loop; every decision about gaps, retries and latency
reads clock.now(), so advancing the simulated clock takes effect at the next sweep or pass.

One iteration never stops the loop: an unexpected error is logged by type name only and the
next iteration runs as usual.
"""

import logging
from dataclasses import dataclass
from time import monotonic, sleep

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, db, gaps, notifications
from app.formats import whole_seconds

logger = logging.getLogger("worker")


def run_iteration(engine: Engine, *, sweep: bool) -> None:
    """One pass: the sweeper if it is due, then deliveries (so a new gap's notification goes
    out in the same iteration)."""
    if sweep:
        gaps.sweep(engine)  # logs its own summary line (spec 3.8)
    notifications.deliver_due(engine)


def run_forever(engine: Engine) -> None:
    next_sweep = monotonic()
    while True:
        now = monotonic()
        sweep = now >= next_sweep
        if sweep:
            next_sweep = now + config.sweep_interval_seconds()
        try:
            run_iteration(engine, sweep=sweep)
        except Exception as exc:
            logger.error("worker iteration failed: type=%s", type(exc).__name__)
        sleep(config.delivery_poll_seconds())


@dataclass(frozen=True)
class HealthValues:
    """The three worker values spec 3.8 asks Part 4's health endpoint to expose, named as the
    /healthz fields in spec 4.6 (decision 97)."""

    sweeper_last_ran_seconds_ago: int | None  # real time; None before the first sweep
    pending_deliveries: int
    oldest_pending_delivery_seconds: int | None  # simulated clock; None when nothing is pending
    # STUB for Phase 4 Part 3: always 0 until Part 3 counts the PENDING escalations more than
    # 60 s past due (Phase 4 spec 3.6, Addendum 9.1).
    overdue_escalations: int = 0


def health_values(engine: Engine) -> HealthValues:
    """Read-only. Seconds since the last sweep (wall clock, from sweeper_heartbeat), the number
    of PENDING deliveries, and the age of the oldest one, measured from its notification's
    created_at on clock.now()."""
    with Session(engine) as session:
        last_ran = session.execute(text("SELECT last_ran_at FROM sweeper_heartbeat")).scalar()
        pending, oldest = session.execute(
            text(
                "SELECT count(*), min(n.created_at) FROM notification_delivery d"
                " JOIN notification n ON n.notification_id = d.notification_id"
                " WHERE d.status = 'PENDING'"
            )
        ).one()
        session.rollback()
    return HealthValues(
        sweeper_last_ran_seconds_ago=(
            None if last_ran is None else whole_seconds(last_ran, clock.wall_now())
        ),
        pending_deliveries=int(pending),
        oldest_pending_delivery_seconds=(
            None if oldest is None else whole_seconds(oldest, clock.now())
        ),
    )


def main() -> None:
    logging.basicConfig(level=config.log_level(), format="%(levelname)s [%(name)s] %(message)s")
    run_forever(db.get_engine())


if __name__ == "__main__":
    main()
