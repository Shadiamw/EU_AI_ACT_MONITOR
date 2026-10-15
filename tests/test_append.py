"""M2: ledger.append, ledger.ensure_seed and audit.record_view, as the application role."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, Row, text
from sqlalchemy.orm import Session

from app import audit, clock, ledger
from app.auth import Principal
from app.ledger import ADVISORY_LOCK_KEY, LedgerEntry
from app.ledger.canonical import (
    GENESIS_PREV_HASH,
    LedgerError,
    Payload,
    canonical_json,
    compute_hash,
    format_recorded_at,
)

SEED_PAYLOAD: Payload = {"systems": ["sys-42"], "threshold_count": "2"}
FLAGGED_PAYLOAD: Payload = {
    "event_id": "evt-7f3a",
    "system_id": "sys-42",
    "metric_name": "accuracy_drift",
    "observed_value": "0.34",
    "threshold_value": "0.30",
    "threshold_version_id": "thr-1",
}
THREAD_TIMEOUT = 30.0


def read_chain(engine: Engine) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT seq, entry_type, actor_id, subject_id, payload, recorded_at,"
                    " prev_hash, hash FROM ledger_entry ORDER BY seq"
                )
            ).all()
        )


def assert_clean_chain(rows: list[Row[Any]], expected_length: int) -> None:
    """seq is 1..N, prev_hash links hold, and every stored hash recomputes (M1 functions)."""
    assert [row.seq for row in rows] == list(range(1, expected_length + 1))
    prev_hash = GENESIS_PREV_HASH
    for row in rows:
        assert row.prev_hash == prev_hash
        canonical = canonical_json(
            seq=row.seq,
            entry_type=row.entry_type,
            actor_id=row.actor_id,
            subject_id=row.subject_id,
            payload=row.payload,
            recorded_at=row.recorded_at,
        )
        assert compute_hash(row.prev_hash, canonical) == row.hash
        prev_hash = row.hash


def append_committed(engine: Engine, payload: Payload | None = None) -> LedgerEntry:
    with Session(engine) as session:
        entry = ledger.append(
            session, "EVENT_FLAGGED", "sys-42", "evt-7f3a", payload or FLAGGED_PAYLOAD
        )
        session.commit()
    return entry


def lock_is_free(engine: Engine) -> bool:
    """Ask from another connection whether the append lock can be taken right now."""
    with engine.connect() as conn:
        free = conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        ).scalar_one()
        conn.rollback()
    return bool(free)


def run_in_threads(count: int, work: Callable[[threading.Barrier], Any]) -> list[Any]:
    """Run work in count threads released together by a barrier; return results or errors."""
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def target(index: int) -> None:
        try:
            results[index] = work(barrier)
        except Exception as exc:  # collected and asserted on by the caller
            results[index] = exc

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_TIMEOUT)
        assert not thread.is_alive(), "thread did not finish (deadlock?)"
    return results


@contextmanager
def open_transaction(engine: Engine) -> Iterator[Session]:
    """A session whose transaction has already started (a statement has run)."""
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        yield session
        session.rollback()


# 1. Chain growth


def test_chain_grows_correctly(app_engine: Engine) -> None:
    entries = [append_committed(app_engine, {"n": n}) for n in range(10)]
    rows = read_chain(app_engine)
    assert_clean_chain(rows, 10)
    assert [entry.seq for entry in entries] == list(range(1, 11))
    assert entries[0].prev_hash == GENESIS_PREV_HASH


def test_several_appends_in_one_transaction(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        first = ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        second = ledger.append(session, "NOTIFICATION_REQUESTED", "sys-42", "evt-1", {})
        session.commit()
    assert (first.seq, second.seq) == (1, 2)
    assert second.prev_hash == first.hash
    assert_clean_chain(read_chain(app_engine), 2)


def test_returned_entry_matches_stored_row(app_engine: Engine) -> None:
    entry = append_committed(app_engine)
    (row,) = read_chain(app_engine)
    assert entry == LedgerEntry(**row._asdict())
    assert set(LedgerEntry.__dataclass_fields__) == {
        "seq",
        "entry_type",
        "actor_id",
        "subject_id",
        "payload",
        "recorded_at",
        "prev_hash",
        "hash",
    }


def test_recorded_at_comes_from_clock(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 9, 27, 10, 5, 0, 250999, tzinfo=UTC))
    entry = append_committed(app_engine)
    assert entry.recorded_at == "2026-09-27T10:05:00.250Z"
    assert read_chain(app_engine)[0].recorded_at == "2026-09-27T10:05:00.250Z"


def test_returned_payload_is_a_copy(app_engine: Engine) -> None:
    payload: Payload = {"list": ["a"]}
    entry = append_committed(app_engine, payload)
    payload["list"] = ["changed"]
    assert entry.payload == {"list": ["a"]}


def test_append_does_not_commit(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        assert session.in_transaction()
        assert read_chain(app_engine) == []  # not visible to others until the caller commits
        session.rollback()


# 2. Rollback and failed appends


def test_rollback_leaves_chain_unchanged_and_seq_is_reused(app_engine: Engine) -> None:
    append_committed(app_engine, {"n": 1})
    second = append_committed(app_engine, {"n": 2})
    before = read_chain(app_engine)

    with Session(app_engine) as session:
        rolled_back = ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", {"n": 3})
        session.rollback()
    assert rolled_back.seq == 3
    assert read_chain(app_engine) == before

    third = append_committed(app_engine, {"n": 4})
    assert third.seq == 3
    assert third.prev_hash == second.hash
    assert_clean_chain(read_chain(app_engine), 3)


@pytest.mark.parametrize(
    ("entry_type", "actor_id", "subject_id", "payload"),
    [
        pytest.param("EVENT_FLAGGED", "sys-42", "evt-1", {"v": 0.34}, id="float-payload"),
        pytest.param("EVENT_FLAGED", "sys-42", "evt-1", {}, id="unknown-entry-type"),
        pytest.param("EVENT_FLAGGED", "", "evt-1", {}, id="empty-actor"),
        pytest.param("EVENT_FLAGGED", "sys-42", "", {}, id="empty-subject"),
        pytest.param("EVENT_FLAGGED", "sys-42", "evt-1", [], id="payload-not-object"),
    ],
)
def test_invalid_input_changes_nothing_and_does_not_hold_lock(
    app_engine: Engine, entry_type: str, actor_id: str, subject_id: str, payload: Any
) -> None:
    append_committed(app_engine)
    before = read_chain(app_engine)
    with open_transaction(app_engine) as session:
        with pytest.raises(LedgerError):
            ledger.append(session, entry_type, actor_id, subject_id, payload)
        # The failed caller's transaction is still open, yet the lock is free and a second
        # connection can append immediately.
        assert lock_is_free(app_engine)
        with Session(app_engine) as other:
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            assert ledger.append(other, "EVENT_VIEWED", "viewer-1", "evt-1", {}).seq == 2
            other.commit()
    assert read_chain(app_engine)[:1] == before
    assert_clean_chain(read_chain(app_engine), 2)


# 3. Lock release


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_lock_is_held_until_the_transaction_ends(app_engine: Engine, finish: str) -> None:
    with Session(app_engine) as session:
        ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        assert not lock_is_free(app_engine)
        if finish == "commit":
            session.commit()
        else:
            session.rollback()
        assert lock_is_free(app_engine)


# 4. The lock serializes


def test_lock_serializes_appends(app_engine: Engine) -> None:
    result: dict[str, LedgerEntry] = {}

    def second_append() -> None:
        with Session(app_engine) as other:
            result["entry"] = ledger.append(other, "EVENT_VIEWED", "viewer-1", "evt-1", {})
            other.commit()

    with Session(app_engine) as session:
        first = ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        thread = threading.Thread(target=second_append)
        thread.start()
        thread.join(0.5)
        assert thread.is_alive(), "second append was not blocked by the first"
        assert "entry" not in result
        committed_at = format_recorded_at(clock.now())
        session.commit()
    thread.join(THREAD_TIMEOUT)
    assert not thread.is_alive()
    second = result["entry"]
    assert second.seq == first.seq + 1
    assert second.prev_hash == first.hash
    # recorded_at is taken after the lock is acquired, so it is no earlier than the commit
    # that released the lock.
    assert second.recorded_at >= committed_at
    assert_clean_chain(read_chain(app_engine), 2)


# 5. Concurrency


@pytest.mark.parametrize("run", [1, 2, 3])
def test_fifty_concurrent_appends_make_a_clean_chain(app_engine: Engine, run: int) -> None:
    def work(barrier: threading.Barrier) -> int:
        with Session(app_engine) as session:
            session.execute(text("SELECT 1"))  # connection open, transaction started
            barrier.wait(THREAD_TIMEOUT)
            entry = ledger.append(session, "EVENT_VIEWED", "viewer-1", f"evt-{run}", {})
            session.commit()
            return entry.seq

    results = run_in_threads(50, work)
    errors = [result for result in results if isinstance(result, BaseException)]
    assert errors == []
    assert sorted(results) == list(range(1, 51))
    assert_clean_chain(read_chain(app_engine), 50)


# 6. ensure_seed


def test_ensure_seed_on_empty_ledger_appends_one_seed(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        assert ledger.ensure_seed(session, SEED_PAYLOAD) is None  # type: ignore[func-returns-value]
        session.commit()
    (row,) = read_chain(app_engine)
    assert (row.seq, row.entry_type, row.actor_id, row.subject_id) == (1, "SEED", "system", None)
    assert row.payload == SEED_PAYLOAD
    assert_clean_chain([row], 1)


def test_ensure_seed_on_non_empty_ledger_appends_nothing(app_engine: Engine) -> None:
    append_committed(app_engine)
    before = read_chain(app_engine)
    for _ in range(2):
        with Session(app_engine) as session:
            ledger.ensure_seed(session, SEED_PAYLOAD)
            session.commit()
    assert read_chain(app_engine) == before


def test_ensure_seed_twice_gives_one_seed(app_engine: Engine) -> None:
    for _ in range(2):
        with Session(app_engine) as session:
            ledger.ensure_seed(session, SEED_PAYLOAD)
            session.commit()
    assert [row.entry_type for row in read_chain(app_engine)] == ["SEED"]


def test_twenty_concurrent_ensure_seed_calls_give_one_seed(app_engine: Engine) -> None:
    def work(barrier: threading.Barrier) -> None:
        with Session(app_engine) as session:
            session.execute(text("SELECT 1"))
            barrier.wait(THREAD_TIMEOUT)
            ledger.ensure_seed(session, SEED_PAYLOAD)
            session.commit()

    results = run_in_threads(20, work)
    assert [result for result in results if isinstance(result, BaseException)] == []
    rows = read_chain(app_engine)
    assert [(row.seq, row.entry_type) for row in rows] == [(1, "SEED")]
    assert_clean_chain(rows, 1)


def test_ensure_seed_rejects_invalid_payload(app_engine: Engine) -> None:
    with Session(app_engine) as session, pytest.raises(LedgerError):
        ledger.ensure_seed(session, {"threshold_count": 2.0})  # type: ignore[dict-item]
    assert read_chain(app_engine) == []


# 7. Isolation


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_append_rejects_other_isolation_levels(app_engine: Engine, level: str) -> None:
    engine = app_engine.execution_options(isolation_level=level)
    with Session(engine) as session:
        with pytest.raises(LedgerError, match="READ COMMITTED"):
            ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        with pytest.raises(LedgerError, match="READ COMMITTED"):
            ledger.ensure_seed(session, SEED_PAYLOAD)
        session.rollback()
    assert read_chain(app_engine) == []
    assert lock_is_free(app_engine)


def test_append_works_at_default_read_committed(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        assert session.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        session.commit()
    assert len(read_chain(app_engine)) == 1


def test_append_runs_as_application_role(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        assert session.execute(text("SELECT current_user")).scalar_one() == "monitor_app"
        ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", FLAGGED_PAYLOAD)
        session.commit()


# 8. record_view


def test_record_view_for_two_principals(app_engine: Engine) -> None:
    viewer = Principal(role="VIEWER", actor_id="viewer-1", system_id=None)
    reviewer = Principal(role="REVIEWER", actor_id="rev-1", system_id=None)
    for principal in (viewer, reviewer):
        with Session(app_engine) as session:
            audit.record_view(session, principal, "evt-7f3a")
            session.commit()
    rows = read_chain(app_engine)
    assert [(r.entry_type, r.actor_id, r.subject_id, r.payload) for r in rows] == [
        ("EVENT_VIEWED", "viewer-1", "evt-7f3a", {}),
        ("EVENT_VIEWED", "rev-1", "evt-7f3a", {}),
    ]
    assert_clean_chain(rows, 2)


# 9. jsonb round trip through append


def test_appended_entries_round_trip_through_jsonb(app_engine: Engine) -> None:
    payloads: list[Payload] = [
        {"accented": "café Zürich", "cjk": "日本語の文字", "emoji": "😀🚀"},
        {"b": {"y": 1, "x": [{"d": 1, "c": 2}]}, "a": "0.340", "big": 2**53 + 1},
        {"s": 'quote" backslash\\ newline\n ctrl\x01 del\x7f ls ', "t": True, "n": None},
    ]
    entries = [append_committed(app_engine, payload) for payload in payloads]
    rows = read_chain(app_engine)
    assert [row.payload for row in rows] == payloads
    assert [row.hash for row in rows] == [entry.hash for entry in entries]
    assert_clean_chain(rows, 3)
