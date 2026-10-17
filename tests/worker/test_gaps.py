"""Part 3: the gap sweeper (REQ-02) and closing a gap on the next sample.

Times are on the fake clock, which starts at T0 = 2026-10-01T09:00:00Z. With the default
configuration the gap timeout is 3 x 300 = 900 seconds: the third missed sample opens a gap
(AC-02; decision 92).
"""

import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import events, gaps, ledger
from tests.api.conftest import ApiClient, append_lock_is_free, as_actor, build_app
from tests.worker.conftest import (
    T0,
    FakeClock,
    add_system,
    app_sessions_waiting_for_a_lock,
    entry_types,
    rows,
)

HARM_NULLS = {
    "health_safety_harm": None,
    "critical_infrastructure_disruption": None,
    "fundamental_rights_infringement": None,
    "property_environmental_damage": None,
}


def ingest_sample(app_engine: Engine, system_id: str, fake_clock: FakeClock) -> str | None:
    """What Part 1's ingest does for Part 3: lock the system row, close any open gap (step 6),
    record the sample time, commit."""
    with Session(app_engine) as session:
        session.execute(
            text("SELECT 1 FROM monitored_system WHERE system_id = :s FOR UPDATE"),
            {"s": system_id},
        )
        closed = gaps.close_if_open(session, system_id)
        session.execute(
            text("UPDATE monitored_system SET last_received_at = :now WHERE system_id = :s"),
            {"now": fake_clock.now(), "s": system_id},
        )
        session.commit()
    return closed


# Timing


def test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(600)  # 2 intervals withheld: AC-02 says no gap
    assert gaps.sweep(app_engine) == []
    fake_clock.advance(299)  # 899 s: one second short of the third missed sample
    assert gaps.sweep(app_engine) == []
    assert entry_types(owner_engine) == []
    fake_clock.advance(1)  # exactly 3 intervals (15 min) withheld: AC-02 says a gap
    opened = gaps.sweep(app_engine)
    assert len(opened) == 1
    assert entry_types(owner_engine) == ["GAP_OPENED", "NOTIFICATION_REQUESTED"]


def test_timeout_follows_the_sampling_interval_and_interval_count(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAMPLING_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("GAP_TIMEOUT_INTERVALS", "2")
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(119)
    assert gaps.sweep(app_engine) == []
    fake_clock.advance(1)
    assert len(gaps.sweep(app_engine)) == 1


def test_a_system_that_never_sent_data_is_measured_from_monitoring_start(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    # No ingest at all: the gap still appears (API contract, GET /v1/events?type=monitoring_gap).
    add_system(owner_engine, "sys-42", started_at=T0)
    fake_clock.advance(901)
    [event_id] = gaps.sweep(app_engine)
    [opened] = rows(
        owner_engine, "SELECT payload FROM ledger_entry WHERE entry_type = 'GAP_OPENED'"
    )
    assert opened["payload"] == {
        "event_id": event_id,
        "system_id": "sys-42",
        "last_received_at": None,
    }
    [gap] = rows(owner_engine, "SELECT last_received_at, silent_since FROM monitoring_gap")
    assert gap == {"last_received_at": None, "silent_since": T0}


def test_only_silent_systems_get_gaps(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-a", last_received_at=T0)
    add_system(owner_engine, "sys-b", last_received_at=T0 + timedelta(seconds=600))
    fake_clock.advance(1000)  # sys-a silent 1000 s, sys-b 400 s
    gaps.sweep(app_engine)
    assert [r["system_id"] for r in rows(owner_engine, "SELECT system_id FROM monitoring_gap")] == [
        "sys-a"
    ]


# What a gap writes


def test_a_gap_writes_the_event_the_gap_row_and_two_ledger_entries(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    last = T0 + timedelta(seconds=10, microseconds=250000)
    add_system(owner_engine, "sys-42", last_received_at=last)
    detected = fake_clock.advance(10.25 + 901)
    [event_id] = gaps.sweep(app_engine)
    assert event_id.startswith("evt-")

    [event] = rows(owner_engine, "SELECT * FROM event")
    assert event == {
        "event_id": event_id,
        "type": "monitoring_gap",
        "system_id": "sys-42",
        "state": "FLAGGED",
        "detected_at": detected,
        "metric_name": None,
        "observed_value": None,
        "threshold_value": None,
        "harm_fields": HARM_NULLS,
        "closed_at": None,
        "metric_type": None,
        "threshold_version_id": None,
        "series_open": False,
        "evidence_bundle": [],
        "disposition": None,
        "deadline_utc": None,
        "revisions": [],
    }
    [gap] = rows(owner_engine, "SELECT * FROM monitoring_gap")
    assert gap == {
        "event_id": event_id,
        "system_id": "sys-42",
        "last_received_at": last,
        "silent_since": last,
        "opened_at": detected,
        "closed_at": None,
        "gap_seconds": None,
    }
    [notification] = rows(owner_engine, "SELECT * FROM notification")
    assert notification["event_id"] == event_id
    assert notification["notification_type"] == "flag_notification"
    assert notification["requested_at"] == detected

    entries = rows(
        owner_engine,
        "SELECT entry_type, actor_id, subject_id, payload FROM ledger_entry ORDER BY seq",
    )
    assert entries == [
        {
            "entry_type": "GAP_OPENED",
            "actor_id": "system",
            "subject_id": event_id,
            "payload": {
                "event_id": event_id,
                "system_id": "sys-42",
                "last_received_at": "2026-10-01T09:00:10.25Z",
            },
        },
        {
            "entry_type": "NOTIFICATION_REQUESTED",
            "actor_id": "system",
            "subject_id": event_id,
            "payload": {
                "notification_id": notification["notification_id"],
                "event_id": event_id,
                "notification_type": "flag_notification",
            },
        },
    ]
    assert ledger.verify_entries(rows(owner_engine, "SELECT * FROM ledger_entry ORDER BY seq"))[
        "valid"
    ]
    assert append_lock_is_free(owner_engine)


def test_gap_entries_are_in_the_event_audit_trail(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    [event_id] = gaps.sweep(app_engine)
    with ApiClient(build_app()) as client:
        response = client.get(f"/v1/events/{event_id}/audit", headers=as_actor("rev-1"))
    assert response.status_code == 200
    assert [(e["actor_id"], e["action"]) for e in response.json()] == [
        ("system", "GAP_OPENED"),
        ("system", "NOTIFICATION_REQUESTED"),
    ]


# One open gap per system


def test_no_second_gap_while_one_is_open(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    assert len(gaps.sweep(app_engine)) == 1
    for _ in range(3):
        fake_clock.advance(3600)
        assert gaps.sweep(app_engine) == []
    assert entry_types(owner_engine) == ["GAP_OPENED", "NOTIFICATION_REQUESTED"]


def test_the_database_refuses_a_second_open_gap(owner_engine: Engine) -> None:
    insert = (
        "INSERT INTO monitoring_gap (event_id, system_id, silent_since, opened_at)"
        " VALUES (:id, 'sys-42', :t, :t)"
    )
    with owner_engine.begin() as conn:
        conn.execute(text(insert), {"id": "evt-1", "t": T0})
    with (
        pytest.raises(IntegrityError, match="uq_monitoring_gap_one_open_per_system"),
        owner_engine.begin() as conn,
    ):
        conn.execute(text(insert), {"id": "evt-2", "t": T0})


def test_concurrent_sweeps_open_exactly_one_gap(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    callers = 5
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(gaps.sweep(app_engine))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    # Hold the system row so every sweeper has listed sys-42 as a candidate and is waiting
    # for the row lock before any of them can open the gap (decision 80's approach).
    with owner_engine.connect() as holder:
        holder.execute(text("SELECT 1 FROM monitored_system WHERE system_id = 'sys-42' FOR UPDATE"))
        threads = [threading.Thread(target=run) for _ in range(callers)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 30
        while True:
            waiting = app_sessions_waiting_for_a_lock(owner_engine)
            if waiting >= callers or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        holder.rollback()
    for thread in threads:
        thread.join()
    assert waiting == callers, f"{waiting} of {callers} sweepers reached the row lock"
    assert errors == []
    # The re-check under the row lock stops the others; the unique index is only a backstop,
    # and hitting it would be logged as a failed sweep.
    assert "gap sweep failed" not in caplog.text
    assert sorted(len(r) for r in results) == [0] * (callers - 1) + [1]
    assert entry_types(owner_engine) == ["GAP_OPENED", "NOTIFICATION_REQUESTED"]
    assert len(rows(owner_engine, "SELECT * FROM event")) == 1


# Errors


def test_a_failure_for_one_system_does_not_stop_the_others(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_system(owner_engine, "sys-a", last_received_at=T0)
    add_system(owner_engine, "sys-b", last_received_at=T0)
    fake_clock.advance(901)
    real_create = events.create_gap_event

    def failing_for_a(session: Session, event_id: str, system_id: str, *args: Any) -> None:
        if system_id == "sys-a":
            raise OperationalError("INSERT", {}, Exception("simulated"))
        real_create(session, event_id, system_id, *args)

    monkeypatch.setattr(events, "create_gap_event", failing_for_a)
    opened = gaps.sweep(app_engine)
    assert len(opened) == 1
    assert [r["system_id"] for r in rows(owner_engine, "SELECT system_id FROM monitoring_gap")] == [
        "sys-b"
    ]
    assert "gap sweep failed: system_id=sys-a type=OperationalError" in caplog.text
    assert "simulated" not in caplog.text
    assert append_lock_is_free(owner_engine)

    # The next sweep retries sys-a.
    monkeypatch.setattr(events, "create_gap_event", real_create)
    assert len(gaps.sweep(app_engine)) == 1
    assert len(rows(owner_engine, "SELECT * FROM monitoring_gap")) == 2


def test_a_system_removed_between_listing_and_locking_is_skipped(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    fake_clock.advance(901)
    with Session(app_engine) as session:
        assert gaps._open_gap(session, "sys-missing", timedelta(seconds=900)) is None
        session.rollback()
    assert entry_types(owner_engine) == []


# Closing


def test_the_next_sample_closes_the_gap(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    [event_id] = gaps.sweep(app_engine)
    resumed = fake_clock.advance(98.9)  # data back 999.9 s after the last sample
    assert ingest_sample(app_engine, "sys-42", fake_clock) == event_id

    [closed] = rows(
        owner_engine,
        "SELECT actor_id, subject_id, payload FROM ledger_entry WHERE entry_type = 'GAP_CLOSED'",
    )
    assert closed == {
        "actor_id": "system",
        "subject_id": event_id,
        "payload": {"event_id": event_id, "system_id": "sys-42", "gap_seconds": 999},
    }
    assert type(closed["payload"]["gap_seconds"]) is int
    [gap] = rows(owner_engine, "SELECT closed_at, gap_seconds FROM monitoring_gap")
    assert gap == {"closed_at": resumed, "gap_seconds": 999}
    [event] = rows(owner_engine, "SELECT state, closed_at FROM event")
    assert event == {"state": "CLOSED", "closed_at": resumed}
    assert entry_types(owner_engine) == ["GAP_OPENED", "NOTIFICATION_REQUESTED", "GAP_CLOSED"]


def test_after_closing_a_new_silence_opens_a_new_gap(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    [first] = gaps.sweep(app_engine)
    fake_clock.advance(100)
    ingest_sample(app_engine, "sys-42", fake_clock)
    fake_clock.advance(899)
    assert gaps.sweep(app_engine) == []  # measured from the new sample
    fake_clock.advance(1)
    [second] = gaps.sweep(app_engine)
    assert second != first
    assert entry_types(owner_engine) == [
        "GAP_OPENED",
        "NOTIFICATION_REQUESTED",
        "GAP_CLOSED",
        "GAP_OPENED",
        "NOTIFICATION_REQUESTED",
    ]


def test_close_if_open_without_an_open_gap_writes_nothing(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(60)
    assert ingest_sample(app_engine, "sys-42", fake_clock) is None
    assert entry_types(owner_engine) == []
    assert rows(owner_engine, "SELECT * FROM event") == []


def test_close_if_open_does_not_commit(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    gaps.sweep(app_engine)
    with Session(app_engine) as session:
        assert gaps.close_if_open(session, "sys-42") is not None
        session.rollback()  # the ingest failed later on
    assert entry_types(owner_engine) == ["GAP_OPENED", "NOTIFICATION_REQUESTED"]
    [gap] = rows(owner_engine, "SELECT closed_at FROM monitoring_gap")
    assert gap["closed_at"] is None
