"""STUB for Part 1/4 (database sessions): the application engine and per-request sessions.

The engine connects as the application role (DATABASE_URL) at READ COMMITTED, which append
requires (addendum rule 5; docs/decisions.md 26). Each request gets one Session, so one
transaction. Handlers commit explicitly before they return (decision 25); this dependency
never commits. It rolls back if the handler raises and always closes the session, which
also rolls back anything left uncommitted.

LOCK_TIMEOUT, when set, is the connection-level lock_timeout (decision 110). The test suite
leaves it unset so a test can hold the append lock until every caller is waiting.
"""

import os
import re
from collections.abc import Iterator
from functools import cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app import config

# Enough connections for every request the default thread pool (40 threads) can run at once.
POOL_SIZE = 10
MAX_OVERFLOW = 40

# A positive integer and a unit. Anything else is refused, so the value cannot add a second
# setting after the timeout (for example a statement_timeout).
_LOCK_TIMEOUT = re.compile(r"^[1-9][0-9]*(ms|min|s)$")


def connect_args() -> dict[str, str]:
    """libpq options for the application role. Empty unless LOCK_TIMEOUT is set."""
    raw = os.environ.get("LOCK_TIMEOUT")
    if raw is None or raw == "":
        return {}
    if _LOCK_TIMEOUT.fullmatch(raw) is None:
        raise RuntimeError(
            "environment variable LOCK_TIMEOUT must be a positive integer plus ms, s or min"
        )
    return {"options": f"-c lock_timeout={raw}"}


@cache
def get_engine() -> Engine:
    return create_engine(
        config.database_url(),
        isolation_level="READ COMMITTED",
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        connect_args=connect_args(),
    )


def get_session() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
