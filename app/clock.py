"""Part 4 clock (spec 4.5, decision 133).

Shared rule: all code gets the current time from clock.now(); nothing else reads the system
clock. Callers use `clock.now()` through the module, so tests can patch `app.clock.now`.

In ENVIRONMENT=test, now() is wall time plus clock_state.offset_seconds. Outside test it is
wall time only, with no database read. wall_now() is always real UTC time, for the sweeper
heartbeat (spec 3.3).
"""

from datetime import UTC, datetime, timedelta
from functools import cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app import config


@cache
def _offset_engine() -> Engine:
    # A short-lived pool of one. Reading the offset uses its own connection (open item 9).
    return create_engine(config.database_url(), poolclass=NullPool)


def offset_seconds() -> int:
    """The test-environment offset, or 0 when the table has no row yet."""
    with _offset_engine().connect() as conn:
        value = conn.execute(text("SELECT offset_seconds FROM clock_state")).scalar()
    return 0 if value is None else int(value)


def now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    moment = datetime.now(UTC)
    if not config.is_test_environment():
        return moment
    return moment + timedelta(seconds=offset_seconds())


def wall_now() -> datetime:
    """Real (wall-clock) UTC time, never simulated. Only for the sweeper heartbeat."""
    return datetime.now(UTC)
