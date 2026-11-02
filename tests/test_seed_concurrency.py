"""Two startup seeds at once both succeed (app/runtime.py, decision 153).

The api and worker processes each run the seed at start (`python -m app.runtime api|worker`,
decision 109), and compose starts them together. Every seeded table but api_key used
INSERT ... ON CONFLICT DO NOTHING, and the ledger's SEED is serialized by the append lock, but
api_key was DELETE-all then INSERT, so the second seed's INSERT collided with the first's rows
(UniqueViolation on api_key_pkey) and that process exited. The test holds the first seed open,
waits until the second is blocked on it, then commits the first: the order the race needs.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import config
from app.auth import key_digest
from tools.seed_part1 import seed

WAIT_SECONDS = 30


def _blocked_backends(engine: Engine) -> int:
    with engine.connect() as conn:
        count: int = conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).scalar_one()
    return count


def _state(engine: Engine) -> tuple[list[tuple[str, str]], list[str]]:
    with engine.connect() as conn:
        keys = [
            (str(row[0]), str(row[1]))
            for row in conn.execute(text("SELECT actor_id, role FROM api_key ORDER BY actor_id"))
        ]
        entry_types = conn.execute(text("SELECT entry_type FROM ledger_entry ORDER BY seq"))
        entries = [str(entry_type) for entry_type in entry_types.scalars()]
    return keys, entries


@pytest.mark.parametrize("already_seeded", [False, True], ids=["fresh", "after-prepare"])
def test_two_concurrent_seeds_both_succeed(owner_engine: Engine, already_seeded: bool) -> None:
    if already_seeded:  # what `prepare` leaves before api and worker start
        with Session(owner_engine) as session:
            seed(session)
            session.commit()
    errors: list[BaseException] = []

    def second() -> None:
        try:
            with Session(owner_engine) as session:
                seed(session)
                session.commit()
        except BaseException as exc:  # reported below, not swallowed
            errors.append(exc)

    first = Session(owner_engine)
    try:
        seed(first)  # uncommitted: holds its api_key rows
        thread = threading.Thread(target=second)
        thread.start()
        deadline = time.monotonic() + WAIT_SECONDS
        while _blocked_backends(owner_engine) == 0:
            assert thread.is_alive(), f"the second seed did not wait for the first: {errors}"
            assert time.monotonic() < deadline, "the second seed never blocked on the first"
            time.sleep(0.05)
        first.commit()
    finally:
        first.close()
    thread.join(WAIT_SECONDS)
    assert not thread.is_alive()
    assert errors == [], f"{type(errors[0]).__name__}: {errors[0]}"
    keys, entries = _state(owner_engine)
    assert keys == [("rev-1", "REVIEWER"), ("sys-42", "SYSTEM"), ("viewer-1", "VIEWER")]
    assert entries == ["SEED"]


def test_stale_and_changed_keys_are_replaced(owner_engine: Engine) -> None:
    """The seed still replaces earlier rows, so the stored keys always match the environment:
    a key no longer configured is removed and a configured key gets its configured role."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_key (key_hash, role, actor_id, system_id) VALUES"
                " ('stale', 'REVIEWER', 'rev-old', NULL), (:reviewer, 'VIEWER', 'someone', NULL)"
            ),
            {"reviewer": key_digest(config.reviewer_key())},
        )
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    keys, _ = _state(owner_engine)
    assert keys == [("rev-1", "REVIEWER"), ("sys-42", "SYSTEM"), ("viewer-1", "VIEWER")]
