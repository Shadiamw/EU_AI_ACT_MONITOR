"""Part 3: the gap sweeper (REQ-02) and closing a gap on the next sample.

Times are on the fake clock, which starts at T0 = 2026-10-01T09:00:00Z. With the default
configuration the gap timeout is 3 x 300 = 900 seconds: the third missed sample opens a gap
(AC-02; decision 92).
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import clock, gaps, ledger
from tests.api.conftest import ApiClient, append_lock_is_free, as_actor, build_app
from tests.worker.conftest import (
    T0,
    FakeClock,
    add_system,
    entry_types,
    rows,
)

HARM_FIELD_KEYS = [
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "health_safety_harm",
    "property_environmental_damage",
]


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


def test_timeout_comes_from_the_system_row(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 3.2: gap threshold = gap_timeout_intervals x sampling_interval_seconds, per system:
    2 x 60 = 120 s for sys-fast, while sys-42 keeps 3 x 300 = 900 s."""
    add_system(owner_engine, "sys-fast", sampling_interval_s=60, gap_timeout_intervals=2)
    add_system(owner_engine, "sys-42")
    fake_clock.advance(119)
    assert gaps.sweep(app_engine) == []
    fake_clock.advance(1)
    [fast_gap] = gaps.sweep(app_engine)
    fake_clock.advance(779)  # 899 s for sys-42
    assert gaps.sweep(app_engine) == []
    fake_clock.advance(1)
    [slow_gap] = gaps.sweep(app_engine)
    by_system = {r["event_id"]: r["system_id"] for r in rows(owner_engine, "SELECT * FROM event")}
    assert by_system == {fast_gap: "sys-fast", slow_gap: "sys-42"}


def test_env_sampling_settings_do_not_change_the_sweeper(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 5: SAMPLING_INTERVAL_SECONDS and GAP_TIMEOUT_INTERVALS only seed sys-42."""
    monkeypatch.setenv("SAMPLING_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("GAP_TIMEOUT_INTERVALS", "2")
    add_system(owner_engine, "sys-42")  # row: 300 s x 3
    fake_clock.advance(899)
    assert gaps.sweep(app_engine) == []
    fake_clock.advance(1)
    assert len(gaps.sweep(app_engine)) == 1


def test_a_seeded_system_with_no_samples_is_measured_from_seed_time(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    # No ingest at all: last_received_at is the seed time (spec 3.4 step 2), and the gap still
    # appears (API contract, GET /v1/events?type=monitoring_gap).
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    [event_id] = gaps.sweep(app_engine)
    [opened] = rows(
        owner_engine, "SELECT payload FROM ledger_entry WHERE entry_type = 'GAP_OPENED'"
    )
    assert opened["payload"] == {
        "event_id": event_id,
        "system_id": "sys-42",
        "last_received_at": "2026-10-01T09:00:00Z",
    }


def test_only_silent_systems_get_gaps(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-a", last_received_at=T0)
    add_system(owner_engine, "sys-b", last_received_at=T0 + timedelta(seconds=600))
    fake_clock.advance(1000)  # sys-a silent 1000 s, sys-b 400 s
    gaps.sweep(app_engine)
    assert [r["system_id"] for r in rows(owner_engine, "SELECT system_id FROM event")] == ["sys-a"]


# What a gap writes


def test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    last = T0 + timedelta(seconds=10, microseconds=250000)
    add_system(owner_engine, "sys-42", last_received_at=last)
    detected = fake_clock.advance(10.25 + 901)
    [event_id] = gaps.sweep(app_engine)
    assert event_id.startswith("evt-")

    # Spec 3.4 table: detected 2026-10-01T09:15:11.25Z. Six calendar months is 2027-04-01, only
    # 182 days later, so the 183-day floor applies: 2027-04-02T09:15:11.25Z.
    [event] = rows(owner_engine, "SELECT * FROM event")
    assert event == {
        "event_id": event_id,
        "system_id": "sys-42",
        "type": "monitoring_gap",
        "metric_name": None,
        "observed_value": None,
        "threshold_value": None,
        "threshold_version_id": None,
        "detected_at": detected,
        "received_at": detected,
        "state": "FLAGGED",
        "deadline_utc": None,
        "evidence_bundle": None,
        "evidence_sha256": None,
        "retain_until": datetime(2027, 4, 2, 9, 15, 11, 250000, tzinfo=UTC),
    }
    harm = rows(owner_engine, "SELECT * FROM harm_field ORDER BY field_key")
    assert harm == [
        {
            "event_id": event_id,
            "field_key": key,
            "status": "NOT_APPLICABLE",
            "evidence": None,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        for key in HARM_FIELD_KEYS
    ]
    [notification] = rows(owner_engine, "SELECT * FROM notification")
    assert notification["event_id"] == event_id
    assert notification["notification_type"] == "flag_notification"
    assert notification["created_at"] == detected

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
    """Spec 1.7: a partial unique index allows one FLAGGED monitoring_gap event per system."""
    insert = (
        "INSERT INTO event (event_id, system_id, type, detected_at, received_at, state,"
        " retain_until) VALUES (:id, 'sys-42', 'monitoring_gap', :t, :t, :state, :t)"
    )
    with owner_engine.begin() as conn:
        conn.execute(text(insert), {"id": "evt-1", "t": T0, "state": "FLAGGED"})
        conn.execute(text(insert), {"id": "evt-0", "t": T0, "state": "CLOSED"})
    with (
        pytest.raises(IntegrityError, match="uq_event_one_open_gap_per_system"),
        owner_engine.begin() as conn,
    ):
        conn.execute(text(insert), {"id": "evt-2", "t": T0, "state": "FLAGGED"})


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
    start = threading.Barrier(callers)

    def run() -> None:
        try:
            start.wait(30)
            results.append(gaps.sweep(app_engine))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    # A sweeper either skips the locked row or re-checks under the lock and finds the open
    # gap; the unique index is only a backstop, and hitting it would be logged as a failure.
    assert "gap sweep failed" not in caplog.text
    assert sorted(len(r) for r in results) == [0] * (callers - 1) + [1]
    assert entry_types(owner_engine) == ["GAP_OPENED", "NOTIFICATION_REQUESTED"]
    assert len(rows(owner_engine, "SELECT * FROM event")) == 1


def test_a_locked_system_row_is_skipped_not_waited_for(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 3.4 step 1: lock each system row, and skip it if another process holds it."""
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)
    results: list[list[str]] = []
    with owner_engine.connect() as holder:
        holder.execute(text("SELECT 1 FROM monitored_system WHERE system_id = 'sys-42' FOR UPDATE"))
        sweeper = threading.Thread(target=lambda: results.append(gaps.sweep(app_engine)))
        sweeper.start()
        sweeper.join(10)
        finished_while_held = not sweeper.is_alive()
        holder.rollback()
    sweeper.join()
    assert finished_while_held, "the sweeper waited for the held row instead of skipping it"
    assert results == [[]]
    assert entry_types(owner_engine) == []
    # Released: the next sweep opens the gap.
    assert len(gaps.sweep(app_engine)) == 1


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
    real_insert = gaps._insert_gap_event

    def failing_for_a(session: Session, event_id: str, system_id: str, *args: Any) -> None:
        if system_id == "sys-a":
            raise OperationalError("INSERT", {}, Exception("simulated"))
        real_insert(session, event_id, system_id, *args)

    monkeypatch.setattr(gaps, "_insert_gap_event", failing_for_a)
    opened = gaps.sweep(app_engine)
    assert len(opened) == 1
    assert [r["system_id"] for r in rows(owner_engine, "SELECT system_id FROM event")] == ["sys-b"]
    assert "gap sweep failed: system_id=sys-a type=OperationalError" in caplog.text
    assert "simulated" not in caplog.text
    assert append_lock_is_free(owner_engine)

    # The next sweep retries sys-a.
    monkeypatch.setattr(gaps, "_insert_gap_event", real_insert)
    assert len(gaps.sweep(app_engine)) == 1
    assert len(rows(owner_engine, "SELECT * FROM event")) == 2


def test_a_system_removed_between_listing_and_locking_is_skipped(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    fake_clock.advance(901)
    with Session(app_engine) as session:
        assert gaps._sweep_system(session, "sys-missing") == (False, None)
        session.rollback()
    assert entry_types(owner_engine) == []


# Closing


def test_recovery_closes_the_gap_and_counts_from_detection(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=T0)
    fake_clock.advance(901)  # the gap is detected (received_at) at T0 + 901 s
    [event_id] = gaps.sweep(app_engine)
    fake_clock.advance(98.9)  # data back 98.9 s after detection
    assert ingest_sample(app_engine, "sys-42", fake_clock) == event_id

    # Spec 3.4: the outage length from the gap's received_at to now, in whole seconds: 98.
    [closed] = rows(
        owner_engine,
        "SELECT actor_id, subject_id, payload FROM ledger_entry WHERE entry_type = 'GAP_CLOSED'",
    )
    assert closed == {
        "actor_id": "system",
        "subject_id": event_id,
        "payload": {"event_id": event_id, "system_id": "sys-42", "gap_seconds": 98},
    }
    assert type(closed["payload"]["gap_seconds"]) is int
    [event] = rows(owner_engine, "SELECT state FROM event")
    assert event == {"state": "CLOSED"}
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
    [event] = rows(owner_engine, "SELECT state FROM event")
    assert event["state"] == "FLAGGED"


# Heartbeat and logging


def test_sweep_writes_the_wall_clock_heartbeat(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.3: after every sweep, sweeper_heartbeat.last_ran_at holds the real time, not the
    simulated clock (here the fake clock is at 2026-10-01; the wall clock is pinned)."""
    real_time = datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)
    monkeypatch.setattr(clock, "wall_now", lambda: real_time)
    assert gaps.sweep(app_engine) == []  # even a sweep with no systems
    assert rows(owner_engine, "SELECT * FROM sweeper_heartbeat") == [
        {"singleton": True, "last_ran_at": real_time}
    ]
    later = real_time + timedelta(seconds=60)
    monkeypatch.setattr(clock, "wall_now", lambda: later)
    gaps.sweep(app_engine)
    assert rows(owner_engine, "SELECT last_ran_at FROM sweeper_heartbeat") == [
        {"last_ran_at": later}
    ]


def test_sweep_logs_systems_examined_and_gaps_opened(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.8: each sweep logs the systems examined and the gaps opened."""
    add_system(owner_engine, "sys-a", last_received_at=T0)
    add_system(owner_engine, "sys-b", last_received_at=T0 + timedelta(seconds=600))
    add_system(owner_engine, "sys-c", last_received_at=T0)
    fake_clock.advance(901)
    with caplog.at_level(logging.INFO, logger="worker"):
        gaps.sweep(app_engine)
    assert "gap sweep: systems_examined=3 gaps_opened=2" in caplog.text
