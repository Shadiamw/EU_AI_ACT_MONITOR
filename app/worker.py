"""The worker process (Part 3): the gap sweeper every minute, and the delivery pass.

Run it with `python -m app.worker`. It connects as the application role (DATABASE_URL), like
the API. Wall-clock time only paces the loop; every decision about gaps, retries and latency
reads clock.now(), so advancing the simulated clock takes effect at the next sweep or pass.

One iteration never stops the loop: an unexpected error is logged by type name only and the
next iteration runs as usual.
"""

import logging
from time import monotonic, sleep

from sqlalchemy import Engine

from app import config, db, gaps, notifications

logger = logging.getLogger("worker")


def run_iteration(engine: Engine, *, sweep: bool) -> None:
    """One pass: the sweeper if it is due, then deliveries (so a new gap's notification goes
    out in the same iteration)."""
    if sweep:
        opened = gaps.sweep(engine)
        if opened:
            logger.info("gap sweep opened %d gap(s)", len(opened))
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    run_forever(db.get_engine())


if __name__ == "__main__":
    main()
