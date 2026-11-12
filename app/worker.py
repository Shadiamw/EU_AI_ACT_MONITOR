"""The worker process (Part 3): the gap sweeper every minute, the reminder pass and the delivery
pass, then the nightly ledger verification when it is due (Phase 5 spec 6.1).

Run it with `python -m app.worker`. It connects as the application role (DATABASE_URL), like
the API. Wall-clock time only paces the loop; every decision about gaps, retries and latency
reads clock.now(), so advancing the simulated clock takes effect at the next sweep or pass.

One iteration never stops the loop: an unexpected error is logged by type name only and the
next iteration runs as usual.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic, sleep

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, db, escalations, gaps, ledger_ops, notifications
from app.formats import whole_seconds

logger = logging.getLogger("worker")


def run_iteration(engine: Engine, *, sweep: bool) -> None:
    """One pass: the sweeper if it is due, then due reminders, then deliveries (so a new gap's
    notification or a reminder goes out in the same iteration; Phase 4 spec 3.6), then the
    nightly verification if it is due, then the alert step. An error in the reminder pass is
    logged by type name and the deliveries still run."""
    if sweep:
        gaps.sweep(engine)  # logs its own summary line (spec 3.8)
    try:
        escalations.fire_due(engine)
    except Exception as exc:
        logger.error("reminder pass failed: type=%s", type(exc).__name__)
    notifications.deliver_due(engine)
    run_nightly(engine)
    run_alerts(engine)


def run_nightly(engine: Engine) -> None:
    """The nightly ledger verification (Phase 5 spec 6.1; Part 3 decision 302), after the
    deliveries so it never delays them. It uses its own connections and transactions; an error
    is logged by type name only and never reaches the rest of the iteration."""
    try:
        if ledger_ops.nightly_due(engine):
            ledger_ops.run_verification(engine, ledger_ops.NIGHTLY)
    except Exception as exc:
        logger.error("nightly verification failed: type=%s", type(exc).__name__)


def run_alerts(engine: Engine) -> None:
    """The alert step (Phase 5 spec 7.3), after the nightly step. It reads in its own
    transaction and logs each raised alert once per code and subject per UTC day; an error is
    logged by type name only and never reaches the rest of the iteration."""
    from app import ops  # inside the function: ops imports this module for health_values

    try:
        ops.raise_alerts(engine)
    except Exception as exc:
        logger.error("alert step failed: type=%s", type(exc).__name__)


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
    """The worker values Part 4's health endpoint exposes, named as the /healthz fields in spec
    4.6 (decision 97), plus overdue_escalations (Phase 4 spec 3.6)."""

    sweeper_last_ran_seconds_ago: int | None  # real time; None before the first sweep
    pending_deliveries: int
    oldest_pending_delivery_seconds: int | None  # simulated clock; None when nothing is pending
    # PENDING escalations more than 60 s past due on the simulated clock. A growing value means
    # the worker is stopped or behind.
    overdue_escalations: int = 0


def health_values(engine: Engine) -> HealthValues:
    """Read-only. Seconds since the last sweep (wall clock, from sweeper_heartbeat), the number
    of PENDING deliveries, the age of the oldest one, measured from its notification's
    created_at on clock.now(), and the overdue escalations."""
    with Session(engine) as session:
        last_ran = session.execute(text("SELECT last_ran_at FROM sweeper_heartbeat")).scalar()
        pending, oldest = session.execute(
            text(
                "SELECT count(*), min(n.created_at) FROM notification_delivery d"
                " JOIN notification n ON n.notification_id = d.notification_id"
                " WHERE d.status = 'PENDING'"
            )
        ).one()
        overdue = session.execute(
            text("SELECT count(*) FROM escalation WHERE status = 'PENDING' AND due_at < :before"),
            {"before": clock.now() - timedelta(seconds=escalations.OVERDUE_AFTER_SECONDS)},
        ).scalar_one()
        session.rollback()
    return HealthValues(
        sweeper_last_ran_seconds_ago=(
            None if last_ran is None else whole_seconds(last_ran, clock.wall_now())
        ),
        pending_deliveries=int(pending),
        oldest_pending_delivery_seconds=(
            None if oldest is None else whole_seconds(oldest, clock.now())
        ),
        overdue_escalations=int(overdue),
    )


def main() -> None:
    logging.basicConfig(level=config.log_level(), format="%(levelname)s [%(name)s] %(message)s")
    config.escalation_percentages()  # a bad value stops start-up (Part 3 decision 196)
    run_forever(db.get_engine())


if __name__ == "__main__":
    main()
