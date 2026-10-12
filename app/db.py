"""STUB for Part 1/4 (database sessions): the application engine and per-request sessions.

The engine connects as the application role (DATABASE_URL) at READ COMMITTED, which append
requires (addendum rule 5; docs/decisions.md 26). Each request gets one Session, so one
transaction. Handlers commit explicitly before they return (decision 25); this dependency
never commits. It rolls back if the handler raises and always closes the session, which
also rolls back anything left uncommitted.
"""

from collections.abc import Iterator
from functools import cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app import config

# Enough connections for every request the default thread pool (40 threads) can run at once.
POOL_SIZE = 10
MAX_OVERFLOW = 40


@cache
def get_engine() -> Engine:
    return create_engine(
        config.database_url(),
        isolation_level="READ COMMITTED",
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
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
