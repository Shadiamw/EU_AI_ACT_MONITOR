"""The pinned lock order of the disposition and reminder transactions (integration decision 222).

Spec 7 and 10, Guide 8: (1) monitored_system, (2) the event row, (3) that event's harm_field,
disposition and escalation rows, (4) notification rows, (5) the ledger lock inside ledger.append.
Disposition and the escalation scheduler never take the monitored_system row. This is the
parts12-verification test 4 (N7), written now that fire_due exists, and paired with it.

Each test watches a transaction from second connections: NOWAIT probes show which rows it holds
at a moment, and pg_locks shows when it waits for a lock someone else holds. Clock:
FakeClock (it patches app.clock.now in this process, including the worker threads here).
Marked integration because it needs Parts 1, 2 and 3's real code (Addendum 9.4).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app import escalations, notifications
from tests.api.conftest import ApiClient, append_lock_is_free
from tests.part1.conftest import client as client
from tests.part1.conftest import fake_clock as fake_clock
from tests.part1.conftest import seeded as seeded
from tests.part2.conftest import ANCHOR, dispose, flag
from tests.worker.conftest import FakeClock

pytestmark = pytest.mark.integration

LOCK_NOT_AVAILABLE = "55P03"
WAIT_SECONDS = 20


def held(engine: Engine, sql: str, **params: Any) -> bool:
    """True if another transaction holds a row that `sql` selects (FOR UPDATE NOWAIT fails)."""
    with engine.connect() as conn:
        try:
            conn.execute(text(f"{sql} FOR UPDATE NOWAIT"), params)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                return True
            raise
        finally:
            conn.rollback()
    return False


def event_held(engine: Engine, event_id: str) -> bool:
    return held(engine, "SELECT 1 FROM event WHERE event_id = :e", e=event_id)


def fields_held(engine: Engine, event_id: str) -> bool:
    return held(engine, "SELECT 1 FROM harm_field WHERE event_id = :e", e=event_id)


def escalations_held(engine: Engine, event_id: str) -> bool:
    return held(engine, "SELECT 1 FROM escalation WHERE event_id = :e", e=event_id)


def system_held(engine: Engine) -> bool:
    return held(engine, "SELECT 1 FROM monitored_system WHERE system_id = 'sys-42'")


def lock_waiters(engine: Engine) -> int:
    """Lock requests not yet granted. pg_locks is readable by any role (pg_stat_activity hides
    another role's wait_event); it is cluster-wide, so one suite per server (Guide 9)."""
    with engine.connect() as conn:
        return int(
            conn.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted")).scalar_one()
        )


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


@contextmanager
def event_row_held(engine: Engine, event_id: str) -> Iterator[Connection]:
    """Another transaction holding the event row, as a concurrent revision or firing would."""
    with engine.connect() as conn:
        conn.execute(
            text("SELECT 1 FROM event WHERE event_id = :e FOR UPDATE"), {"e": event_id}
        ).one()
        try:
            yield conn
        finally:
            conn.rollback()


class Gate:
    """Wraps a function so that its call stops until released, inside the caller's transaction."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        assert self.release.wait(WAIT_SECONDS), "never released"
        return self.real(*args, **kwargs)


def in_thread(pool: ThreadPoolExecutor, call: Callable[[], Any]) -> Future[Any]:
    return pool.submit(call)


def dispositioned(client: ApiClient, fake_clock: FakeClock, tier: str = "OTHER_SERIOUS") -> str:
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    response = dispose(client, event_id, tier)
    assert response.status_code == 200, response.text
    return event_id


def test_a_disposition_holds_the_event_and_its_fields_never_the_system_and_appends_last(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopped inside escalations.plan: the event row and the harm fields it wrote are held, the
    monitored_system row is free, and the ledger lock is still free (append comes last)."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    gate = Gate(escalations.plan)
    monkeypatch.setattr(escalations, "plan", gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = in_thread(pool, lambda: dispose(client, event_id, "DEATH"))
        try:
            assert gate.entered.wait(WAIT_SECONDS), "plan was never called"
            assert event_held(owner_engine, event_id)
            assert fields_held(owner_engine, event_id)
            assert not system_held(owner_engine)
            assert append_lock_is_free(owner_engine)
        finally:
            gate.release.set()
        assert future.result(WAIT_SECONDS).status_code == 200


def test_a_revision_takes_the_event_row_before_its_fields_and_escalation_rows(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """While another transaction holds the event row, a revision waits, and meanwhile holds
    none of the event's harm_field or escalation rows: the event row comes first."""
    event_id = dispositioned(client, fake_clock)
    fake_clock.advance(3 * 86400)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with event_row_held(owner_engine, event_id):
            future = in_thread(pool, lambda: dispose(client, event_id, "DEATH"))
            wait_for(lambda: lock_waiters(owner_engine) >= 1, "the revision to wait")
            assert not fields_held(owner_engine, event_id)
            assert not escalations_held(owner_engine, event_id)
            assert not system_held(owner_engine)
        assert future.result(WAIT_SECONDS).status_code == 200


def test_fire_due_takes_the_event_row_before_the_escalation_row(
    client: ApiClient, owner_engine: Engine, app_engine: Engine, fake_clock: FakeClock
) -> None:
    """While another transaction holds the event row, fire_due waits on it and holds no
    escalation row; once released it fires the 50% reminder."""
    event_id = dispositioned(client, fake_clock)
    fake_clock.set(ANCHOR + timedelta(days=8))  # past 50% of the 15-day window
    with ThreadPoolExecutor(max_workers=1) as pool:
        with event_row_held(owner_engine, event_id):
            future = in_thread(pool, lambda: escalations.fire_due(app_engine))
            wait_for(lambda: lock_waiters(owner_engine) >= 1, "fire_due to wait")
            assert not escalations_held(owner_engine, event_id)
            assert not system_held(owner_engine)
        assert future.result(WAIT_SECONDS) == [(event_id, 50)]


def test_fire_due_holds_the_event_and_the_row_never_the_system_and_appends_last(
    client: ApiClient,
    owner_engine: Engine,
    app_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopped inside notifications.enqueue (step 5 of spec 3.4): the event row and the
    escalation row are held, monitored_system is free, and the ledger lock is still free."""
    event_id = dispositioned(client, fake_clock)
    fake_clock.set(ANCHOR + timedelta(days=8))
    gate = Gate(notifications.enqueue)
    monkeypatch.setattr(notifications, "enqueue", gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = in_thread(pool, lambda: escalations.fire_due(app_engine))
        try:
            assert gate.entered.wait(WAIT_SECONDS), "enqueue was never called"
            assert event_held(owner_engine, event_id)
            assert held(
                owner_engine,
                "SELECT 1 FROM escalation WHERE event_id = :e AND pct = 50",
                e=event_id,
            )
            assert not system_held(owner_engine)
            assert append_lock_is_free(owner_engine)
        finally:
            gate.release.set()
        assert future.result(WAIT_SECONDS) == [(event_id, 50)]
