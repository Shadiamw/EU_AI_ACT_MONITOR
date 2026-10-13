"""M6: the whole ledger, end to end, in ONE scenario (decision 74).

The earlier tests check each part on its own. This module follows one ledger from a database
freshly built by migrations 0001 and 0002 through seeding, every entry type, concurrency, a
rollback, the audit and event endpoints, the export and the standalone verifier, tampering,
the two roles, the error contract and a restart, to show the parts work together.

Steps a to j run in file order and share the ledger, so a later step starts where the earlier
one ended. Every append runs as the application role (DATABASE_URL); the owner role only
reads rows for comparison and tampers in step g and h.

Expected values are constants written down from the spec before the first run
(docs/decisions.md 74). The clock is patched to fixed instants, so every recorded_at, and
therefore every hash, is known in advance. Hashes are recomputed here with hashlib and
json.dumps only, never with app code.
"""

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg.errors
import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import clock, ledger
from app.db import get_engine
from app.ledger import verify_ledger
from tests.api.conftest import (
    FORBIDDEN,
    MALFORMED,
    NOT_FOUND,
    SEED_PAYLOAD,
    UNAUTHORIZED,
    ApiClient,
    append_lock_is_free,
    as_actor,
    build_app,
    count_entries,
    owner_rows,
    trigger_enabled,
)
from tests.api.test_cross_verify import Results, three_results
from tests.conftest import alembic_config
from tests.db_helpers import TRIGGER_NAME, trigger_disabled

GENESIS = "0" * 64
# Migration 0002 raises "ledger_entry is append-only: % is not allowed" with TG_OP.
APPEND_ONLY = "ledger_entry is append-only: {operation} is not allowed"


def valid(count: int) -> dict[str, Any]:
    return {"valid": True, "entries_checked": count, "first_mismatch": None}


def invalid(count: int, seq: int) -> dict[str, Any]:
    return {"valid": False, "entries_checked": count, "first_mismatch": str(seq)}


def at(hour: int, minute: int, second: int, microsecond: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, second, microsecond, tzinfo=UTC)


# a. The SEED, appended with the clock at 09:00:00.000000.
SEED_TIME = at(9, 0, 0, 0)
SEED_RECORDED_AT = "2026-10-13T09:00:00.000Z"

# b. One entry per type in the spec's table (SEED excluded), plus a null subject_id. Entry seq
# N is appended with the clock at 09:00:N.567890, so its recorded_at is 09:00:N.567Z
# (truncated, never rounded; decision 7).
EVENT = "evt-sys"
GAP = "evt-gap"
TYPED_ENTRIES: list[tuple[int, str, str, str | None, dict[str, Any]]] = [
    (
        2,
        "EVENT_FLAGGED",
        "sys-42",
        EVENT,
        {
            "event_id": EVENT,
            "system_id": "sys-42",
            "metric_name": "accuracy_drift",
            "observed_value": "0.34",
            "threshold_value": "0.30",
            "threshold_version_id": "thr-1",
        },
    ),
    (
        3,
        "NOTIFICATION_REQUESTED",
        "sys-42",
        EVENT,
        {"notification_id": "ntf-1", "event_id": EVENT, "notification_type": "flag_notification"},
    ),
    (
        4,
        "NOTIFICATION_DELIVERED",
        "system",
        EVENT,
        {
            "notification_id": "ntf-1",
            "webhook_id": "wh-1",
            "attempt": 1,
            "latency_seconds": 2,
            "within_bound": True,
        },
    ),
    (
        5,
        "GAP_OPENED",
        "system",
        GAP,
        {"event_id": GAP, "system_id": "sys-42", "last_received_at": "2026-10-13T08:45:00.000Z"},
    ),
    (
        6,
        "NOTIFICATION_FAILED",
        "system",
        GAP,
        {"notification_id": "ntf-2", "webhook_id": "wh-1", "attempts": 3, "last_status": 500},
    ),
    (7, "GAP_CLOSED", "system", GAP, {"event_id": GAP, "system_id": "sys-42", "gap_seconds": 900}),
    (
        8,
        "WEBHOOK_REGISTERED",
        "rev-1",
        "wh-1",
        {
            "webhook_id": "wh-1",
            "url": "https://ex.ample/é/日本",
            "event_types": ["flag_notification", "reminder"],
        },
    ),
    (9, "EVENT_VIEWED", "viewer-1", EVENT, {}),
    (10, "AUDIT_VIEWED", "rev-1", EVENT, {}),
    # No spec entry type other than SEED has a null subject, and decision 11 means append
    # accepts one for any type; this exercises null in the canonical form through append.
    (11, "GAP_CLOSED", "system", None, {"event_id": GAP, "system_id": "sys-42", "gap_seconds": 0}),
]
ALL_TYPES = {
    "EVENT_FLAGGED",
    "GAP_OPENED",
    "GAP_CLOSED",
    "NOTIFICATION_REQUESTED",
    "NOTIFICATION_DELIVERED",
    "NOTIFICATION_FAILED",
    "WEBHOOK_REGISTERED",
    "EVENT_VIEWED",
    "AUDIT_VIEWED",
}

# c. 50 concurrent appends take seq 12 to 61.
CONCURRENT = 50
AFTER_CONCURRENT = 61

# d. The rolled-back append and its committed replacement both get seq 62.
REUSED_SEQ = 62


# e. The audit trail of evt-sys from step b, then the entries step e adds.
def item(actor_id: str, action: str, timestamp: str) -> dict[str, str]:
    return {"actor_id": actor_id, "action": action, "timestamp": timestamp}


EVENT_TRAIL = [
    item("sys-42", "EVENT_FLAGGED", "2026-10-13T09:00:02.567Z"),
    item("sys-42", "NOTIFICATION_REQUESTED", "2026-10-13T09:00:03.567Z"),
    item("system", "NOTIFICATION_DELIVERED", "2026-10-13T09:00:04.567Z"),
    item("viewer-1", "EVENT_VIEWED", "2026-10-13T09:00:09.567Z"),
    item("rev-1", "AUDIT_VIEWED", "2026-10-13T09:00:10.567Z"),
]
GAP_TRAIL = [  # seq 5, 6 and 7; seq 11 has no subject, so it is not in any trail
    item("system", "GAP_OPENED", "2026-10-13T09:00:05.567Z"),
    item("system", "NOTIFICATION_FAILED", "2026-10-13T09:00:06.567Z"),
    item("system", "GAP_CLOSED", "2026-10-13T09:00:07.567Z"),
]
AUDIT_TIME = at(9, 30, 0, 999)  # -> 09:30:00.000Z
VIEW_TIME = at(9, 30, 0, 123456)  # -> 09:30:00.123Z
LAST_AUDIT_TIME = at(9, 31, 0, 999999)  # -> 09:31:00.999Z, never rounded up to 09:31:01.000Z
EVENT_TRAIL_AFTER_VIEW = [
    *EVENT_TRAIL,
    item("rev-1", "AUDIT_VIEWED", "2026-10-13T09:30:00.000Z"),  # seq 63
    item("viewer-1", "EVENT_VIEWED", "2026-10-13T09:30:00.123Z"),  # seq 65
]
TOTAL = 66  # 63 and 64 are the two audits, 65 the view, 66 the last audit

# g. Tamper cases on the 66-entry chain: (name, result, failed check).
TAMPER_EXPECTED = {
    "payload_change_at_5": (invalid(5, 5), "hash"),
    "delete_middle_30": (invalid(30, 31), "seq"),
    "swap_20_and_21": (invalid(20, 20), "prev_hash"),
    "forge_40_consistent_hash": (invalid(41, 41), "prev_hash"),
    "seq_50_becomes_150": (invalid(50, 51), "seq"),
    # Entry 1 removed and entry 2 re-linked to genesis with a consistent hash: only the
    # first-must-be-1 rule catches it at entry 2 (decision 79).
    "first_row_removed_and_relinked": (invalid(1, 2), "seq"),
}


def canonical(row: dict[str, Any]) -> str:
    content = {
        k: row[k] for k in ("seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at")
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(prev_hash: str, canonical_form: str) -> str:
    return hashlib.sha256((prev_hash + canonical_form).encode("utf-8")).hexdigest()


def chained(rows: list[dict[str, Any]]) -> None:
    """Every row links to the one before and its hash recomputes with hashlib."""
    previous = GENESIS
    for expected_seq, row in enumerate(rows, start=1):
        assert row["seq"] == expected_seq
        assert row["prev_hash"] == previous
        assert row["hash"] == sha256_hex(row["prev_hash"], canonical(row))
        previous = row["hash"]


def fixed_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setattr(clock, "now", lambda: instant)


def entry_count(owner_engine: Engine) -> int:
    with owner_engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one())


def run_together(
    count: int, work: Callable[[], Any], while_running: Callable[[], None] | None = None
) -> list[Any]:
    """Start `count` threads at once (a barrier) and return their results or raise.

    `while_running`, if given, runs in the main thread after the threads start and before
    they are joined.
    """
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = work()
        except BaseException as error:  # noqa: BLE001 - re-raised below in the main thread
            errors.append(error)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    try:
        if while_running is not None:
            while_running()
    finally:
        for thread in threads:
            thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads), "a worker thread hung"
    if errors:
        raise errors[0]
    return results


def wait_for_lock_waiters(holder: Connection, count: int) -> None:
    """Wait until `count` sessions wait for an advisory lock, then end the holder's transaction.

    pg_locks shows every session's locks to every role.
    """
    deadline = time.monotonic() + 30
    with holder.engine.connect() as watcher:
        while True:
            waiting = watcher.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            ).scalar_one()
            watcher.rollback()
            if waiting >= count or time.monotonic() > deadline:
                break
            time.sleep(0.05)
    holder.rollback()
    assert waiting == count, f"{waiting} of {count} callers reached the append lock"


# Module setup: a fresh database, and this module's own ledger shared by steps a to j.


@pytest.fixture(scope="module", autouse=True)
def fresh_database(owner_engine: Engine) -> Iterator[None]:
    """Rebuild the schema with migrations 0001 to 0003; clean up after the whole scenario."""
    get_engine().dispose()
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003"
    yield
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE ledger_entry ENABLE TRIGGER {TRIGGER_NAME}"))
        conn.execute(text("TRUNCATE ledger_entry"))


@pytest.fixture(autouse=True)
def empty_ledger(owner_engine: Engine) -> Iterator[None]:
    """Overrides the shared pre-test truncate: the steps share one ledger.

    After every step the trigger must be enabled again, whatever the step did.
    """
    yield
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE ledger_entry ENABLE TRIGGER {TRIGGER_NAME}"))
    assert trigger_enabled(owner_engine)


@pytest.fixture(scope="module")
def client() -> Iterator[ApiClient]:
    with ApiClient(build_app()) as api_client:
        yield api_client


def api_verify(client: ApiClient) -> dict[str, Any]:
    response = client.get("/v1/ledger/verify", headers=as_actor("viewer-1"))
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


def append(
    app_engine: Engine, entry_type: str, actor: str, subject: str | None, payload: Any
) -> Any:
    with Session(app_engine) as session:
        entry = ledger.append(session, entry_type, actor, subject, payload)
        session.commit()
    return entry


# a. Empty ledger and a concurrent seed.


def test_a_empty_ledger_then_one_seed_from_20_concurrent_callers(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert api_verify(client) == valid(0)
    export = client.get("/v1/ledger/export", headers=as_actor("rev-1"))
    assert (export.status_code, export.json()) == (200, [])

    fixed_clock(monkeypatch, SEED_TIME)

    def seed_once() -> None:
        with Session(app_engine) as session:
            ledger.ensure_seed(session, SEED_PAYLOAD)
            session.commit()

    # Hold the append lock while the 20 callers start, and release it only once all 20 wait
    # for it. Every caller then reaches the lock before any SEED exists, so the result does
    # not depend on thread timing: a seed that checked for emptiness before taking the lock
    # would append 20 SEEDs here (decision 80).
    with owner_engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": ledger.ADVISORY_LOCK_KEY}
        )
        run_together(20, seed_once, while_running=lambda: wait_for_lock_waiters(holder, 20))

    expected_seed = {
        "seq": 1,
        "entry_type": "SEED",
        "actor_id": "system",
        "subject_id": None,
        "payload": {"systems": ["sys-42"], "threshold_count": "2"},
        "recorded_at": SEED_RECORDED_AT,
        "prev_hash": GENESIS,
    }
    expected_seed["hash"] = sha256_hex(GENESIS, canonical(expected_seed))
    assert owner_rows(owner_engine) == [expected_seed]
    assert api_verify(client) == valid(1)
    assert append_lock_is_free(owner_engine)


# b. Every entry type through the service, each hash recomputed with hashlib.


def test_b_every_entry_type_appends_with_the_expected_hash(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert {entry[1] for entry in TYPED_ENTRIES} == ALL_TYPES
    previous_hash = owner_rows(owner_engine)[0]["hash"]
    for seq, entry_type, actor, subject, payload in TYPED_ENTRIES:
        fixed_clock(monkeypatch, at(9, 0, seq, 567890))
        expected = {
            "seq": seq,
            "entry_type": entry_type,
            "actor_id": actor,
            "subject_id": subject,
            "payload": payload,
            "recorded_at": f"2026-10-13T09:00:{seq:02d}.567Z",
            "prev_hash": previous_hash,
        }
        expected["hash"] = sha256_hex(previous_hash, canonical(expected))

        returned = append(app_engine, entry_type, actor, subject, payload)

        assert vars(returned) == expected
        assert owner_rows(owner_engine)[-1] == expected
        previous_hash = expected["hash"]

    rows = owner_rows(owner_engine)
    assert len(rows) == 11
    chained(rows)
    assert "日本" in canonical(rows[7])  # stored and hashed as UTF-8, not \u escapes
    assert rows[10]["subject_id"] is None and '"subject_id":null' in canonical(rows[10])
    assert api_verify(client) == valid(11)


# c. 50 concurrent appends from separate sessions.


def test_c_50_concurrent_appends_stay_contiguous_and_linked(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    # As in step a: all 50 must be waiting for the append lock before any of them appends.
    with owner_engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": ledger.ADVISORY_LOCK_KEY}
        )
        entries = run_together(
            CONCURRENT,
            lambda: append(app_engine, "EVENT_VIEWED", "viewer-1", "evt-load", {}),
            while_running=lambda: wait_for_lock_waiters(holder, CONCURRENT),
        )

    assert sorted(entry.seq for entry in entries) == list(range(12, AFTER_CONCURRENT + 1))
    rows = owner_rows(owner_engine)
    assert [row["seq"] for row in rows] == list(range(1, AFTER_CONCURRENT + 1))
    chained(rows)
    stored = {row["seq"]: row for row in rows}
    for entry in entries:
        assert vars(entry) == stored[entry.seq]
    assert api_verify(client) == valid(AFTER_CONCURRENT)
    assert append_lock_is_free(owner_engine)


# d. A rolled-back append leaves no gap.


def test_d_a_rolled_back_append_leaves_the_chain_and_its_seq_is_reused(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    before = owner_rows(owner_engine)
    with Session(app_engine) as session:
        lost = ledger.append(session, "EVENT_VIEWED", "rev-1", "evt-load", {})
        session.rollback()
    assert lost.seq == REUSED_SEQ
    assert owner_rows(owner_engine) == before
    assert append_lock_is_free(owner_engine)

    kept = append(app_engine, "EVENT_VIEWED", "viewer-1", "evt-load", {})
    assert kept.seq == REUSED_SEQ
    assert kept.prev_hash == before[-1]["hash"]
    chained(owner_rows(owner_engine))
    assert api_verify(client) == valid(REUSED_SEQ)


# e. The audit and event endpoints.


def test_e_audit_lists_the_event_and_a_view_appends_one_entry(
    client: ApiClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_clock(monkeypatch, AUDIT_TIME)
    response = client.get(f"/v1/events/{EVENT}/audit", headers=as_actor("rev-1"))
    assert (response.status_code, response.json()) == (200, EVENT_TRAIL)
    response = client.get(f"/v1/events/{GAP}/audit", headers=as_actor("viewer-1"))
    assert (response.status_code, response.json()) == (200, GAP_TRAIL)

    fixed_clock(monkeypatch, VIEW_TIME)
    response = client.get(f"/v1/events/{EVENT}", headers=as_actor("viewer-1"))
    assert (response.status_code, response.json()) == (200, {"event_id": EVENT})

    rows = owner_rows(owner_engine)
    assert len(rows) == 65
    # Each audit appended one AUDIT_VIEWED, and the view exactly one EVENT_VIEWED.
    assert [
        (r["seq"], r["entry_type"], r["actor_id"], r["subject_id"], r["payload"], r["recorded_at"])
        for r in rows[-3:]
    ] == [
        (63, "AUDIT_VIEWED", "rev-1", EVENT, {}, "2026-10-13T09:30:00.000Z"),
        (64, "AUDIT_VIEWED", "viewer-1", GAP, {}, "2026-10-13T09:30:00.000Z"),
        (65, "EVENT_VIEWED", "viewer-1", EVENT, {}, "2026-10-13T09:30:00.123Z"),
    ]

    fixed_clock(monkeypatch, LAST_AUDIT_TIME)
    response = client.get(f"/v1/events/{EVENT}/audit", headers=as_actor("viewer-1"))
    assert (response.status_code, response.json()) == (200, EVENT_TRAIL_AFTER_VIEW)
    last = owner_rows(owner_engine)[-1]
    assert (last["seq"], last["entry_type"], last["actor_id"], last["recorded_at"]) == (
        TOTAL,
        "AUDIT_VIEWED",
        "viewer-1",
        "2026-10-13T09:31:00.999Z",
    )

    for path in ("/v1/events/evt-nope/audit", "/v1/events/evt-nope"):
        response = client.get(path, headers=as_actor("rev-1"))
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert entry_count(owner_engine) == TOTAL
    chained(owner_rows(owner_engine))


# f. Export, the standalone script, the endpoint and the direct call agree.


def test_f_export_file_script_endpoint_and_direct_call_agree(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    export = client.get("/v1/ledger/export", headers=as_actor("rev-1"))
    assert export.status_code == 200
    assert export.json() == owner_rows(owner_engine)

    results = three_results(client, app_engine, caplog, tmp_path)
    assert results.api == results.direct == results.script.result == valid(TOTAL)
    assert (results.script.code, results.script.stderr) == (0, "")
    assert results.server_checks == []


# g. Tampering, detected identically by the endpoint, the direct call and the script.


def forged_40(owner_engine: Engine) -> list[str]:
    with owner_engine.connect() as conn:
        row = dict(conn.execute(text("SELECT * FROM ledger_entry WHERE seq = 40")).one()._mapping)
    row["actor_id"] = "rev-1"
    forged_hash = sha256_hex(row["prev_hash"], canonical(row))
    return [f"UPDATE ledger_entry SET actor_id = 'rev-1', hash = '{forged_hash}' WHERE seq = 40"]


def first_row_removed_and_relinked(owner_engine: Engine) -> list[str]:
    with owner_engine.connect() as conn:
        row = dict(conn.execute(text("SELECT * FROM ledger_entry WHERE seq = 2")).one()._mapping)
    row["prev_hash"] = GENESIS
    relinked_hash = sha256_hex(GENESIS, canonical(row))
    return [
        "DELETE FROM ledger_entry WHERE seq = 1",
        f"UPDATE ledger_entry SET prev_hash = '{GENESIS}', hash = '{relinked_hash}' WHERE seq = 2",
    ]


TAMPERS: dict[str, Callable[[Engine], list[str]]] = {
    "payload_change_at_5": lambda _: [
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{system_id}', '\"sys-99\"')"
        " WHERE seq = 5"
    ],
    "delete_middle_30": lambda _: ["DELETE FROM ledger_entry WHERE seq = 30"],
    "swap_20_and_21": lambda _: [
        "UPDATE ledger_entry SET seq = 1000000 WHERE seq = 20",
        "UPDATE ledger_entry SET seq = 20 WHERE seq = 21",
        "UPDATE ledger_entry SET seq = 21 WHERE seq = 1000000",
    ],
    "forge_40_consistent_hash": forged_40,
    "seq_50_becomes_150": lambda _: ["UPDATE ledger_entry SET seq = 150 WHERE seq = 50"],
    "first_row_removed_and_relinked": first_row_removed_and_relinked,
}


def restore(owner_engine: Engine, rows: list[dict[str, Any]]) -> None:
    """Put the snapshot back exactly, as the owner (TRUNCATE is allowed; addendum rule 1)."""
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entry"))
        conn.execute(
            text(
                "INSERT INTO ledger_entry"
                " (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)"
                " VALUES (:seq, :entry_type, :actor_id, :subject_id, CAST(:payload AS jsonb),"
                " :recorded_at, :prev_hash, :hash)"
            ),
            [{**row, "payload": json.dumps(row["payload"], ensure_ascii=False)} for row in rows],
        )


@pytest.mark.parametrize("case", list(TAMPERS))
def test_g_tampering_is_detected_the_same_way_by_all_three(
    case: str,
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    snapshot = owner_rows(owner_engine)
    assert len(snapshot) == TOTAL
    expected_result, expected_check = TAMPER_EXPECTED[case]
    try:
        with trigger_disabled(owner_engine) as conn:
            for statement in TAMPERS[case](owner_engine):
                conn.execute(text(statement))
        assert trigger_enabled(owner_engine)
        results: Results = three_results(client, app_engine, caplog, tmp_path)
    finally:
        restore(owner_engine, snapshot)

    assert results.api == results.direct == results.script.result == expected_result
    assert results.script.code == 1
    # The endpoint and the direct call each log one mismatch line (decision 60).
    assert results.server_checks == [expected_check, expected_check]
    assert results.script.check == expected_check
    assert owner_rows(owner_engine) == snapshot
    assert api_verify(client) == valid(TOTAL)


# h. What each role may do to the table.


def test_h_roles_app_inserts_and_reads_only_owner_is_blocked_by_the_trigger(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    head = owner_rows(owner_engine)[-1]
    next_row = {
        "seq": TOTAL + 1,
        "entry_type": "EVENT_VIEWED",
        "actor_id": "viewer-1",
        "subject_id": EVENT,
        "payload": {},
        "recorded_at": "2026-10-13T09:40:00.000Z",
        "prev_hash": head["hash"],
    }
    next_row["hash"] = sha256_hex(head["hash"], canonical(next_row))
    with app_engine.connect() as conn:
        inserted = conn.execute(
            text(
                "INSERT INTO ledger_entry"
                " (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)"
                " VALUES (:seq, :entry_type, :actor_id, :subject_id, '{}', :recorded_at,"
                " :prev_hash, :hash)"
            ),
            next_row,
        )
        assert inserted.rowcount == 1
        assert conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one() == TOTAL + 1
        conn.rollback()
        assert conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one() == TOTAL

    for statement in (
        "UPDATE ledger_entry SET actor_id = 'rev-1' WHERE seq = 2",
        "DELETE FROM ledger_entry WHERE seq = 2",
        "TRUNCATE ledger_entry",
    ):
        with app_engine.connect() as conn, pytest.raises(DBAPIError) as denied:
            conn.execute(text(statement))
        assert isinstance(denied.value.orig, psycopg.errors.InsufficientPrivilege), statement

    for operation, statement in (
        ("UPDATE", "UPDATE ledger_entry SET actor_id = 'rev-1' WHERE seq = 2"),
        ("DELETE", "DELETE FROM ledger_entry WHERE seq = 2"),
    ):
        with owner_engine.connect() as conn, pytest.raises(DBAPIError) as blocked:
            conn.execute(text(statement))
        assert isinstance(blocked.value.orig, psycopg.errors.RaiseException), statement
        assert APPEND_ONLY.format(operation=operation) in str(blocked.value.orig)

    assert trigger_enabled(owner_engine)
    assert entry_count(owner_engine) == TOTAL
    assert api_verify(client) == valid(TOTAL)


# i. The error contract.


def test_i_errors_use_the_contract_bodies_and_change_nothing(
    client: ApiClient, owner_engine: Engine
) -> None:
    before = owner_rows(owner_engine)
    cases: list[tuple[str, str, dict[str, str], int, dict[str, str]]] = [
        ("GET", "/v1/ledger/verify", {}, 401, UNAUTHORIZED),
        ("GET", "/v1/ledger/export", {}, 401, UNAUTHORIZED),
        ("GET", f"/v1/events/{EVENT}/audit", {}, 401, UNAUTHORIZED),
        ("GET", "/v1/ledger/verify", as_actor("sys-42"), 403, FORBIDDEN),
        ("GET", "/v1/ledger/export", as_actor("sys-42"), 403, FORBIDDEN),
        ("GET", f"/v1/events/{EVENT}/audit", as_actor("sys-42"), 403, FORBIDDEN),
        ("GET", "/v1/events/evt-nope/audit", as_actor("viewer-1"), 404, NOT_FOUND),
        ("GET", "/_test/typed?count=abc", as_actor("viewer-1"), 400, MALFORMED),
        ("POST", "/v1/ledger/verify", as_actor("viewer-1"), 404, NOT_FOUND),
        ("POST", "/v1/ledger/export", as_actor("viewer-1"), 404, NOT_FOUND),
    ]
    for method, path, headers, status, body in cases:
        response = client.request(method, path, headers=headers)
        assert (response.status_code, response.json()) == (status, body), (method, path)
    assert owner_rows(owner_engine) == before


# j. A restart: a second engine and app on the same database.


def test_j_a_restart_appends_no_second_seed_and_the_chain_stays_valid(
    owner_engine: Engine,
) -> None:
    before = owner_rows(owner_engine)
    first_engine = get_engine()
    first_engine.dispose()
    get_engine.cache_clear()
    restarted = get_engine()
    assert restarted is not first_engine

    with Session(restarted) as session:
        ledger.ensure_seed(session, SEED_PAYLOAD)
        session.commit()

    assert count_entries(owner_engine, "SEED") == 1
    assert owner_rows(owner_engine) == before
    with ApiClient(build_app()) as new_client:
        assert api_verify(new_client) == valid(TOTAL)
    with Session(restarted) as session:
        assert dict(verify_ledger(session)) == valid(TOTAL)
        session.rollback()
