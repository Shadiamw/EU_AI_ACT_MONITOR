"""Part 3 fix round, audit 2 finding B2: a test reset and a verification run never block each
other (decisions 300 and 302).

The completeness audit saw platform.reset()'s TRUNCATE wait on a NIGHTLY run's holder
connection (idle in transaction, holding the due-check's lock on ledger_verification_run) while
the run's checks connection waited behind the TRUNCATE: a cycle PostgreSQL cannot see, so the
service hung. It also saw PostgreSQL detect a deadlock between the TRUNCATE and the worker's
alert-step read and abort the reset with a 500. Two tests, each approved beyond the 18 spec
tests: the first reproduces the hang's shape deterministically (bounded by thread timeouts,
with stuck backends terminated so the test run itself cannot hang); the second makes the
reset's TRUNCATE fail with SQLSTATE 40P01 (deadlock_detected), raised by PostgreSQL itself.
"""

import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app import clock, ledger_ops, platform, worker
from app.ledger.verify import verify_ledger
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.ledger_ops.test_alerts import insert_run

# Past the default NIGHTLY_VERIFY_HOUR_UTC (2), so a NIGHTLY run is due (decision 302).
WALL = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
# Every wait in these tests is bounded; a reset normally takes well under a second.
BOUND_SECONDS = 20.0

DEADLOCK = (
    "DO $$ BEGIN RAISE EXCEPTION 'deadlock detected (raised by the test)'"
    " USING ERRCODE = 'deadlock_detected'; END $$"
)


def reset_count(owner_engine: Engine) -> int:
    with owner_engine.connect() as conn:
        value = conn.execute(text("SELECT reset_count FROM platform_counter")).scalar()
    return 0 if value is None else int(value)


def count(owner_engine: Engine, table: str) -> int:
    with owner_engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def a_lock_wait_by_the_owner(owner_engine: Engine) -> bool:
    """True when a backend of the owner role (the reset) waits for any lock."""
    with owner_engine.connect() as conn:
        waiting = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
                " WHERE NOT l.granted AND a.usename = current_user"
                " AND a.datname = current_database()"
            )
        ).scalar_one()
    return int(waiting) > 0


def terminate_stuck_backends(owner_engine: Engine) -> list[int]:
    """Clean-up after a hang: end every other backend of this database that waits for a lock
    or sits idle in a transaction, so the threads can finish and later tests can run."""
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT pid, pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                " AND (wait_event_type = 'Lock' OR state = 'idle in transaction')"
            )
        ).all()
        conn.commit()
    return [int(row.pid) for row in rows]


def test_reset_during_a_running_verification_does_not_hang(
    seeded: None,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("NIGHTLY_VERIFY_HOUR_UTC", raising=False)
    monkeypatch.setattr(clock, "wall_now", lambda: WALL)
    insert_run(owner_engine, "lvr-before", WALL.replace(hour=1))  # a MANUAL run to be emptied
    before = reset_count(owner_engine)

    # The run's first check waits until released: the run is "in progress".
    started, release = threading.Event(), threading.Event()

    def waiting_verify(session: Any) -> Any:
        started.set()
        release.wait(timeout=BOUND_SECONDS)
        return verify_ledger(session)

    monkeypatch.setattr(ledger_ops, "verify_ledger", waiting_verify)
    results: dict[str, Any] = {}

    def run() -> None:
        try:
            results["run"] = ledger_ops.run_verification(app_engine, ledger_ops.NIGHTLY)
        except BaseException as exc:  # reported below, never silent
            results["run"] = exc

    def reset() -> None:
        try:
            platform.reset()
            results["reset"] = "done"
        except BaseException as exc:
            results["reset"] = exc

    runner = threading.Thread(target=run, daemon=True)
    resetter = threading.Thread(target=reset, daemon=True)
    runner.start()
    assert started.wait(timeout=BOUND_SECONDS), "the verification run never reached its checks"
    resetter.start()
    # The reset is started while the run is in progress, and is waiting for a lock.
    for _ in range(int(BOUND_SECONDS * 10)):
        if a_lock_wait_by_the_owner(owner_engine) or not resetter.is_alive():
            break
        time.sleep(0.1)
    release.set()

    runner.join(timeout=BOUND_SECONDS)
    resetter.join(timeout=BOUND_SECONDS)
    stuck = [name for name, t in (("run", runner), ("reset", resetter)) if t.is_alive()]
    if stuck:
        ended = terminate_stuck_backends(owner_engine)
        runner.join(timeout=BOUND_SECONDS)
        resetter.join(timeout=BOUND_SECONDS)
        app_engine.dispose()
        pytest.fail(
            f"hung: {', '.join(stuck)} did not finish within {BOUND_SECONDS:.0f} s"
            f" (terminated backends {ended})"
        )

    # Both finished: the run completed first and was stored, then the reset emptied the tables
    # (its own row included) and counted itself once. The reset waited for the run instead of
    # meeting it in a lock cycle: it never had to retry.
    assert results["reset"] == "done", results["reset"]
    retried = [r.getMessage() for r in caplog.records if r.getMessage().startswith("reset retried")]
    assert retried == []
    assert isinstance(results["run"], dict), results["run"]
    assert results["run"]["trigger"] == "NIGHTLY"
    assert reset_count(owner_engine) == before + 1
    for table in ("ledger_verification_run", "event", "metric_sample", "notification"):
        assert count(owner_engine, table) == 0, table


def test_reset_retries_after_a_detected_deadlock(
    seeded: None, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each TRUNCATE the reset sends is swapped, while `failures` lasts, for a statement on which
    # PostgreSQL raises deadlock_detected (SQLSTATE 40P01), as the detector does for a victim.
    truncates: list[str] = []
    failures = {"left": 1}
    admin_engine = platform._admin_engine

    def deadlocking_admin_engine() -> Engine:
        engine = admin_engine()

        @event.listens_for(engine, "before_cursor_execute", retval=True)
        def swap(
            conn: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> tuple[str, Any]:
            if statement.startswith("TRUNCATE"):
                truncates.append(statement)
                if failures["left"] > 0:
                    failures["left"] -= 1
                    return DEADLOCK, parameters
            return statement, parameters

        return engine

    monkeypatch.setattr(platform, "_admin_engine", deadlocking_admin_engine)

    # One detected deadlock: the reset retries and succeeds, counted exactly once.
    insert_run(owner_engine, "lvr-before", WALL)
    before = reset_count(owner_engine)
    platform.reset()
    assert len(truncates) == 2
    assert reset_count(owner_engine) == before + 1
    assert count(owner_engine, "ledger_verification_run") == 0

    # A deadlock on every attempt: after the bounded retries the reset raises the error, and
    # nothing changed (one transaction per attempt).
    truncates.clear()
    failures["left"] = 1000
    insert_run(owner_engine, "lvr-kept", WALL)
    with pytest.raises(DBAPIError) as raised:
        platform.reset()
    assert getattr(raised.value.orig, "sqlstate", None) == "40P01"
    attempts: int = getattr(platform, "RESET_ATTEMPTS", 0)  # the bound, set by the code
    assert attempts >= 2
    assert len(truncates) == attempts
    assert reset_count(owner_engine) == before + 1
    assert count(owner_engine, "ledger_verification_run") == 1

    # The reset lock was released after the failure: the next reset runs.
    failures["left"] = 0
    platform.reset()
    assert reset_count(owner_engine) == before + 2
    assert count(owner_engine, "ledger_verification_run") == 0


def test_reset_lock_timeout_breaks_an_invisible_cycle(
    seeded: None,
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit 3, R2: a caller holds a table lock on one connection and waits in Python for a
    second connection, whose read queues behind the reset's TRUNCATE. PostgreSQL cannot see
    that cycle; the reset's SET LOCAL lock_timeout makes the TRUNCATE give way after about 2 s,
    the caller finishes, and the retried reset succeeds, counted once."""
    # Connections with no lock_timeout of their own, so only the reset's can break the cycle.
    plain = create_engine(app_engine.url, poolclass=NullPool)
    holding, release = threading.Event(), threading.Event()
    results: dict[str, Any] = {}
    before = reset_count(owner_engine)

    def caller() -> None:
        try:
            with plain.connect() as first:
                first.execute(text("SELECT count(*) FROM notification_delivery")).scalar_one()
                holding.set()  # AccessShare on notification_delivery, held until commit
                release.wait(timeout=BOUND_SECONDS)
                with plain.connect() as second:  # queues behind the TRUNCATE
                    second.execute(text("SELECT count(*) FROM notification_delivery")).scalar()
                    second.commit()
                first.commit()
            results["caller"] = "done"
        except BaseException as exc:
            results["caller"] = exc

    def reset() -> None:
        try:
            platform.reset()
            results["reset"] = "done"
        except BaseException as exc:
            results["reset"] = exc

    caller_thread = threading.Thread(target=caller, daemon=True)
    resetter = threading.Thread(target=reset, daemon=True)
    caller_thread.start()
    assert holding.wait(timeout=BOUND_SECONDS), "the caller never took its lock"
    started = time.monotonic()
    resetter.start()
    # The reset's TRUNCATE waits for the caller's lock; then the caller goes on to its second
    # connection, whose read queues behind the TRUNCATE.
    for _ in range(int(BOUND_SECONDS * 10)):
        if a_lock_wait_by_the_owner(owner_engine) or not resetter.is_alive():
            break
        time.sleep(0.1)
    release.set()

    caller_thread.join(timeout=BOUND_SECONDS)
    resetter.join(timeout=BOUND_SECONDS)
    elapsed = time.monotonic() - started
    stuck = [name for name, t in (("caller", caller_thread), ("reset", resetter)) if t.is_alive()]
    if stuck:
        ended = terminate_stuck_backends(owner_engine)
        caller_thread.join(timeout=BOUND_SECONDS)
        resetter.join(timeout=BOUND_SECONDS)
        plain.dispose()
        pytest.fail(
            f"hung: {', '.join(stuck)} did not finish within {BOUND_SECONDS:.0f} s"
            f" (terminated backends {ended})"
        )
    plain.dispose()

    assert results["caller"] == "done", results["caller"]
    assert results["reset"] == "done", results["reset"]
    assert reset_count(owner_engine) == before + 1
    # The TRUNCATE gave way on its lock timeout (55P03) and was retried.
    retried = [r.getMessage() for r in caplog.records if r.getMessage().startswith("reset retried")]
    assert retried and all("sqlstate=55P03" in line for line in retried), retried
    assert elapsed >= 1.5, elapsed  # it waited about the 2 s lock timeout first


def test_dashboard_does_not_hold_locks_while_it_waits_for_another_connection(
    client: ApiClient,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 3, R1 (inventory row 2): GET /v1/ops/dashboard must not hold table locks on its
    request session while it waits for worker.health_values on another connection. Each such
    call is paused while a reset runs; the reset's own lock timeout is switched off, so only
    the dashboard's structure can keep the two from waiting on each other for good."""
    monkeypatch.setattr(platform, "RESET_LOCK_TIMEOUT", "0")  # 0: no lock timeout
    real_health_values = worker.health_values
    entered: list[threading.Event] = []
    released: list[threading.Event] = []
    guard = threading.Lock()

    def paused_health_values(engine: Engine) -> Any:
        with guard:
            here, go = threading.Event(), threading.Event()
            entered.append(here)
            released.append(go)
        here.set()
        go.wait(timeout=BOUND_SECONDS)
        return real_health_values(engine)

    monkeypatch.setattr(worker, "health_values", paused_health_values)
    before = reset_count(owner_engine)
    results: dict[str, Any] = {}
    resets: list[threading.Thread] = []

    def dashboard() -> None:
        try:
            results["dashboard"] = client.get(
                "/v1/ops/dashboard", headers=bearer("viewer-1")
            ).status_code
        except BaseException as exc:
            results["dashboard"] = exc

    def reset() -> None:
        try:
            platform.reset()
        except BaseException as exc:
            results.setdefault("reset errors", []).append(exc)

    viewer = threading.Thread(target=dashboard, daemon=True)
    viewer.start()
    deadline = time.monotonic() + BOUND_SECONDS
    handled = 0
    # Each time the dashboard is about to open its second connection: start a reset, let its
    # TRUNCATE queue (or finish), then let the second connection go on.
    while viewer.is_alive() and time.monotonic() < deadline:
        with guard:
            pending = entered[handled] if handled < len(entered) else None
        if pending is None or not pending.is_set():
            time.sleep(0.05)
            continue
        resetter = threading.Thread(target=reset, daemon=True)
        resets.append(resetter)
        resetter.start()
        for _ in range(int(BOUND_SECONDS * 10)):
            if a_lock_wait_by_the_owner(owner_engine) or not resetter.is_alive():
                break
            time.sleep(0.1)
        released[handled].set()
        handled += 1

    viewer.join(timeout=max(0.0, deadline - time.monotonic()))
    for resetter in resets:
        resetter.join(timeout=BOUND_SECONDS if not viewer.is_alive() else 0)
    stuck = (["dashboard"] if viewer.is_alive() else []) + [
        f"reset {n}" for n, t in enumerate(resets, start=1) if t.is_alive()
    ]
    if stuck:
        ended = terminate_stuck_backends(owner_engine)
        for go in released:
            go.set()
        viewer.join(timeout=BOUND_SECONDS)
        for resetter in resets:
            resetter.join(timeout=BOUND_SECONDS)
        pytest.fail(
            f"hung: {', '.join(stuck)} did not finish within {BOUND_SECONDS:.0f} s"
            f" (terminated backends {ended})"
        )

    assert results["dashboard"] == 200, results["dashboard"]
    assert "reset errors" not in results, results.get("reset errors")
    assert resets, "the dashboard never called worker.health_values"
    assert reset_count(owner_engine) == before + len(resets)
