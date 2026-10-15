"""Part 3: notifications (REQ-04) and the delivery pass.

Deliveries go to a real HTTP receiver on 127.0.0.1. Times are on the fake clock (T0 =
2026-10-01T09:00:00Z); the default bound is 900 seconds and the default attempt limit is 5.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import clock, gaps, ledger, notifications, worker
from app.notifications import retry_delay_seconds
from tests.api.conftest import append_lock_is_free
from tests.worker.conftest import (
    T0,
    FakeClock,
    Receiver,
    add_event,
    add_webhook,
    app_sessions_in_transaction,
    closed_port_url,
    entry_types,
    rows,
)


def request_notification(app_engine: Engine, owner_engine: Engine, event_id: str = "evt-1") -> str:
    """What the ingest or the sweeper does: create the event, enqueue in the same transaction,
    then commit. The event row here is written by the owner first (Part 1 is a stub)."""
    add_event(owner_engine, event_id)
    with Session(app_engine) as session:
        notification_id = notifications.enqueue(session, event_id)
        session.commit()
    return notification_id


def delivery(owner_engine: Engine) -> dict[str, Any]:
    [row] = rows(owner_engine, "SELECT * FROM notification_delivery")
    return row


def ledger_payloads(owner_engine: Engine, entry_type: str) -> list[Any]:
    return [
        r["payload"]
        for r in rows(
            owner_engine,
            "SELECT payload FROM ledger_entry WHERE entry_type = :t ORDER BY seq",
            t=entry_type,
        )
    ]


# enqueue


def test_enqueue_records_the_request_and_routes_nothing(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 6 and 3.5: enqueue inserts the notification and appends NOTIFICATION_REQUESTED;
    routing to webhooks is the delivery pass's job. Actor `system` (decision 93)."""
    add_webhook(owner_engine, "wh-flag", "http://127.0.0.1:9/a", ["flag_notification"])
    notification_id = request_notification(app_engine, owner_engine)
    assert notification_id.startswith("ntf-")

    entries = rows(owner_engine, "SELECT actor_id, subject_id, payload FROM ledger_entry")
    assert entries == [
        {
            "actor_id": "system",
            "subject_id": "evt-1",
            "payload": {
                "notification_id": notification_id,
                "event_id": "evt-1",
                "notification_type": "flag_notification",
            },
        }
    ]
    assert rows(owner_engine, "SELECT * FROM notification") == [
        {
            "notification_id": notification_id,
            "event_id": "evt-1",
            "notification_type": "flag_notification",
            "created_at": T0,
        }
    ]
    assert rows(owner_engine, "SELECT * FROM notification_delivery") == []


def test_the_pass_routes_one_delivery_per_subscribed_webhook(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_webhook(owner_engine, "wh-flag", closed_port_url(), ["flag_notification"])
    add_webhook(owner_engine, "wh-both", closed_port_url(), ["reminder", "flag_notification"])
    add_webhook(owner_engine, "wh-reminder", closed_port_url(), ["reminder"])
    request_notification(app_engine, owner_engine)
    notifications._route(app_engine)
    deliveries = rows(
        owner_engine,
        "SELECT webhook_id, status, attempt_count, next_attempt_at, lease_until"
        " FROM notification_delivery ORDER BY webhook_id",
    )
    assert deliveries == [
        {
            "webhook_id": wid,
            "status": "PENDING",
            "attempt_count": 0,
            "next_attempt_at": T0,
            "lease_until": None,
        }
        for wid in ("wh-both", "wh-flag")
    ]


def test_no_webhook_leaves_the_notification_unrouted_until_one_is_registered(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.9 'No webhook registered': the notification stays unrouted, and registering a
    webhook afterwards delivers it."""
    notification_id = request_notification(app_engine, owner_engine)
    for _ in range(3):
        assert notifications.deliver_due(app_engine) == 0
        fake_clock.advance(60)
    assert rows(owner_engine, "SELECT * FROM notification_delivery") == []
    add_webhook(owner_engine, "wh-late", receiver.url, ["flag_notification"])
    assert notifications.deliver_due(app_engine) == 1
    [call] = receiver.calls
    assert call.body["notification_id"] == notification_id
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED", "NOTIFICATION_DELIVERED"]


def test_a_routed_notification_is_not_sent_to_a_webhook_registered_later(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.5 step 1: later-registered webhooks do not receive notifications already routed."""
    add_webhook(owner_engine, "wh-first", receiver.url + "?wh=first", ["flag_notification"])
    request_notification(app_engine, owner_engine)
    receiver.statuses = [500]  # the first attempt fails, so the delivery is still open
    assert notifications.deliver_due(app_engine) == 1
    add_webhook(owner_engine, "wh-later", receiver.url + "?wh=later", ["flag_notification"])
    fake_clock.advance(10)
    assert notifications.deliver_due(app_engine) == 1
    assert [c.path for c in receiver.calls] == ["/hook?wh=first", "/hook?wh=first"]
    assert [r["webhook_id"] for r in rows(owner_engine, "SELECT * FROM notification_delivery")] == [
        "wh-first"
    ]


def test_enqueue_refuses_an_unknown_type_and_writes_nothing(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_event(owner_engine)
    with Session(app_engine) as session:
        with pytest.raises(ValueError):
            notifications.enqueue(session, "evt-1", "email")
        session.rollback()
    assert entry_types(owner_engine) == []
    assert append_lock_is_free(owner_engine)


def test_enqueue_does_not_commit(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_event(owner_engine)
    with Session(app_engine) as session:
        notifications.enqueue(session, "evt-1")
        session.rollback()
    assert entry_types(owner_engine) == []
    assert rows(owner_engine, "SELECT * FROM notification") == []


# Delivery


def test_delivery_body_is_exactly_the_spec_shape(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.5: {notification_type, notification_id, event_id, system_id, type, detected_at,
    sent_at}, where type is threshold_breach or monitoring_gap. Timestamps in the response
    format (spec 4). The breach was detected at 10:05:00Z (contract Example 1's timestamp) and
    received 3 s later; the gap event is detected and received at 10:20:00Z."""
    breach_detected = datetime(2026, 9, 27, 10, 5, 0, tzinfo=UTC)
    gap_detected = datetime(2026, 9, 27, 10, 20, 0, tzinfo=UTC)
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    add_event(
        owner_engine,
        "evt-7f3a",
        detected_at=breach_detected,
        received_at=breach_detected + timedelta(seconds=3),
    )
    add_event(
        owner_engine,
        "evt-gap",
        event_type="monitoring_gap",
        detected_at=gap_detected,
        received_at=gap_detected,
    )
    ids = {}
    with Session(app_engine) as session:
        for event_id in ("evt-7f3a", "evt-gap"):
            ids[event_id] = notifications.enqueue(session, event_id)
        session.commit()
    fake_clock.set(datetime(2026, 9, 27, 10, 20, 3, tzinfo=UTC))
    assert notifications.deliver_due(app_engine) == 2
    bodies = sorted((c.body for c in receiver.calls), key=lambda b: b["event_id"])
    assert bodies == [
        {
            "notification_type": "flag_notification",
            "notification_id": ids["evt-7f3a"],
            "event_id": "evt-7f3a",
            "system_id": "sys-42",
            "type": "threshold_breach",
            "detected_at": "2026-09-27T10:05:00Z",
            "sent_at": "2026-09-27T10:20:03Z",
        },
        {
            "notification_type": "flag_notification",
            "notification_id": ids["evt-gap"],
            "event_id": "evt-gap",
            "system_id": "sys-42",
            "type": "monitoring_gap",
            "detected_at": "2026-09-27T10:20:00Z",
            "sent_at": "2026-09-27T10:20:03Z",
        },
    ]


def test_delivery_appends_delivered_with_integer_latency_and_boolean_bound(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    notification_id = request_notification(app_engine, owner_engine)
    fake_clock.advance(7.9)  # the pass runs 7.9 s after detection

    # Checked inside the HTTP call: no application transaction is open and nobody holds the
    # ledger lock (integration_notes.md 4.3).
    receiver.during = lambda: (
        app_sessions_in_transaction(owner_engine),
        append_lock_is_free(owner_engine),
    )
    assert notifications.deliver_due(app_engine) == 1

    [call] = receiver.calls
    assert call.observed == (0, True)
    assert call.path == "/hook"
    # Spec 3.5 body; the pass runs at T0 + 7.9 s, the breach event was detected at T0.
    assert call.body == {
        "notification_type": "flag_notification",
        "notification_id": notification_id,
        "event_id": "evt-1",
        "system_id": "sys-42",
        "type": "threshold_breach",
        "detected_at": "2026-10-01T09:00:00Z",
        "sent_at": "2026-10-01T09:00:07.9Z",
    }
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert payload == {
        "notification_id": notification_id,
        "webhook_id": "wh-1",
        "attempt": 1,
        "latency_seconds": 7,
        "within_bound": True,
    }
    assert type(payload["latency_seconds"]) is int
    assert payload["within_bound"] is True
    [entry] = rows(
        owner_engine,
        "SELECT actor_id, subject_id FROM ledger_entry WHERE entry_type = 'NOTIFICATION_DELIVERED'",
    )
    assert entry == {"actor_id": "system", "subject_id": "evt-1"}
    row = delivery(owner_engine)
    assert (row["status"], row["attempt_count"], row["last_status"]) == ("DELIVERED", 1, "200")
    assert row["delivered_at"] == T0 + timedelta(seconds=7.9)
    assert row["lease_until"] is None

    # Delivered once: later passes find nothing to do.
    fake_clock.advance(3600)
    assert notifications.deliver_due(app_engine) == 0
    assert len(receiver.calls) == 1


@pytest.mark.parametrize(("delay", "within"), [(900, True), (901, False)])
def test_within_bound_compares_latency_with_the_configured_bound(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    delay: int,
    within: bool,
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    fake_clock.advance(delay)
    notifications.deliver_due(app_engine)
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert (payload["latency_seconds"], payload["within_bound"]) == (delay, within)


@pytest.mark.parametrize(("delay", "within"), [(300, True), (301, False)])
def test_ac04_the_bound_is_configurable_to_five_minutes(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
    delay: int,
    within: bool,
) -> None:
    monkeypatch.setenv("NOTIFY_MAX_DELAY_SECONDS", "300")
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    fake_clock.advance(delay)
    notifications.deliver_due(app_engine)
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert (payload["latency_seconds"], payload["within_bound"]) == (delay, within)


def test_each_subscribed_webhook_gets_its_own_delivery(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-a", receiver.url + "?wh=a", ["flag_notification"])
    add_webhook(owner_engine, "wh-b", receiver.url + "?wh=b", ["flag_notification"])
    request_notification(app_engine, owner_engine)
    assert notifications.deliver_due(app_engine) == 2
    assert sorted(c.path for c in receiver.calls) == ["/hook?wh=a", "/hook?wh=b"]
    assert sorted(
        p["webhook_id"] for p in ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    ) == [
        "wh-a",
        "wh-b",
    ]


def test_nothing_is_due_before_a_notification_exists(
    app_engine: Engine, fake_clock: FakeClock
) -> None:
    assert notifications.deliver_due(app_engine) == 0


# Retries and failure


def test_retry_delays_are_10_30_60_120_300() -> None:
    """Spec 3.5 step 5: retry delays after each failure; six attempts in total."""
    assert [retry_delay_seconds(n) for n in range(1, 6)] == [10, 30, 60, 120, 300]
    assert notifications.MAX_ATTEMPTS == 6


@pytest.mark.parametrize("status", [302, 404, 500, 503])
def test_a_non_2xx_answer_schedules_a_retry_and_appends_nothing(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    status: int,
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    receiver.statuses = [status]
    notifications.deliver_due(app_engine)

    row = delivery(owner_engine)
    assert (row["status"], row["attempt_count"], row["last_status"]) == ("PENDING", 1, str(status))
    assert row["lease_until"] is None
    assert row["next_attempt_at"] == T0 + timedelta(seconds=10)
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED"]

    fake_clock.advance(9)
    assert notifications.deliver_due(app_engine) == 0  # not due yet
    fake_clock.advance(1)
    assert notifications.deliver_due(app_engine) == 1
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert (payload["attempt"], payload["latency_seconds"]) == (2, 10)
    assert len(receiver.calls) == 2


def test_permanent_failure_stops_after_six_attempts(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.9 'Permanent failure': a receiver that always fails ends FAILED after six
    attempts, with one NOTIFICATION_FAILED and no further tries. Attempts start at 0, 10, 40,
    100, 220 and 520 s (delays 10, 30, 60, 120, 300)."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    notification_id = request_notification(app_engine, owner_engine)
    receiver.default_status = 500
    started = []
    for delay in [0, 10, 30, 60, 120, 300]:
        fake_clock.advance(delay)
        if delay:
            fake_clock.advance(-1)
            assert notifications.deliver_due(app_engine) == 0  # one second early
            fake_clock.advance(1)
        started.append(int((fake_clock.now() - T0).total_seconds()))
        assert notifications.deliver_due(app_engine) == 1
    assert started == [0, 10, 40, 100, 220, 520]
    assert ledger_payloads(owner_engine, "NOTIFICATION_FAILED") == [
        {
            "notification_id": notification_id,
            "webhook_id": "wh-1",
            "attempts": 6,
            "last_status": 500,
        }
    ]
    [entry] = rows(
        owner_engine,
        "SELECT actor_id, subject_id FROM ledger_entry WHERE entry_type = 'NOTIFICATION_FAILED'",
    )
    assert entry == {"actor_id": "system", "subject_id": "evt-1"}
    row = delivery(owner_engine)
    assert (row["status"], row["attempt_count"], row["last_status"]) == ("FAILED", 6, "500")
    fake_clock.advance(86400)
    assert notifications.deliver_due(app_engine) == 0
    assert len(receiver.calls) == 6
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED", "NOTIFICATION_FAILED"]


def test_no_response_is_a_failure_with_a_null_status(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_webhook(owner_engine, "wh-1", closed_port_url(), ["flag_notification"])
    notification_id = request_notification(app_engine, owner_engine)
    for delay in [0, 10, 30, 60, 120, 300]:
        fake_clock.advance(delay)
        assert notifications.deliver_due(app_engine) == 1
    assert ledger_payloads(owner_engine, "NOTIFICATION_FAILED") == [
        {
            "notification_id": notification_id,
            "webhook_id": "wh-1",
            "attempts": 6,
            "last_status": None,
        }
    ]
    assert delivery(owner_engine)["last_status"] is None


# Concurrency, leases and lock timeouts


def test_a_claimed_delivery_is_not_taken_by_a_second_pass(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    in_call = threading.Event()
    release = threading.Event()

    def slow_post(url: str, body: dict[str, Any], timeout: int) -> int | None:
        in_call.set()
        assert release.wait(30)
        return 200

    first = threading.Thread(target=notifications.deliver_due, args=(app_engine, slow_post))
    first.start()
    assert in_call.wait(30)
    assert notifications.deliver_due(app_engine) == 0  # leased, lease still running
    release.set()
    first.join()
    assert len(ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")) == 1
    assert receiver.calls == []


def test_an_expired_lease_is_claimed_again_and_the_stale_result_is_ignored(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    notifications._route(app_engine)
    [stale] = notifications._claim(app_engine)  # a pass that crashed before recording
    row = delivery(owner_engine)
    assert (row["status"], row["attempt_count"]) == ("PENDING", 1)
    assert row["lease_until"] == T0 + timedelta(seconds=notifications.LEASE_SECONDS)

    fake_clock.advance(notifications.LEASE_SECONDS - 1)
    assert notifications.deliver_due(app_engine) == 0
    fake_clock.advance(1)
    assert notifications.deliver_due(app_engine) == 1
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert payload["attempt"] == 2

    notifications._record(app_engine, stale, 200)  # the crashed pass's result arrives late
    assert len(ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")) == 1


def test_a_lock_timeout_while_recording_is_rolled_back_and_retried(
    owner_engine: Engine,
    app_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """integration_notes.md section 8, Part 3 item 5."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    timeout_engine = create_engine(
        app_engine.url,
        poolclass=NullPool,
        isolation_level="READ COMMITTED",
        connect_args={"options": "-c lock_timeout=500"},
    )
    try:
        with owner_engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": ledger.ADVISORY_LOCK_KEY}
            )
            with caplog.at_level(logging.WARNING, logger="worker"):
                assert notifications.deliver_due(timeout_engine) == 1  # no exception
            holder.rollback()
        assert "type=OperationalError" in caplog.text
        assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED"]
        row = delivery(owner_engine)
        assert (row["status"], row["lease_until"] is not None) == ("PENDING", True)
        assert app_sessions_in_transaction(owner_engine) == 0

        fake_clock.advance(notifications.LEASE_SECONDS)
        assert notifications.deliver_due(timeout_engine) == 1
    finally:
        timeout_engine.dispose()
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED", "NOTIFICATION_DELIVERED"]
    assert len(receiver.calls) == 2  # at least once: the receiver saw the notification twice


# Latency, late deliveries, logging and health (spec 3.5, 3.8)


def test_latency_counts_from_the_events_received_at(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.5: latency = delivery time minus the event's received_at, not the time the
    notification was requested. Here the event was received 100 s before the request."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    add_event(owner_engine, "evt-1", received_at=T0 - timedelta(seconds=100))
    with Session(app_engine) as session:
        notifications.enqueue(session, "evt-1")
        session.commit()
    fake_clock.advance(5)
    notifications.deliver_due(app_engine)
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert (payload["latency_seconds"], payload["within_bound"]) == (105, True)


def test_late_delivery_is_logged_and_counted(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.5: log a warning and count every late delivery; on-time ones are not counted."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine, "evt-on-time")
    notifications.deliver_due(app_engine)
    before = notifications.late_delivery_count()
    notification_id = request_notification(app_engine, owner_engine, "evt-late")
    fake_clock.advance(901)
    with caplog.at_level(logging.WARNING, logger="worker"):
        notifications.deliver_due(app_engine)
    assert notifications.late_delivery_count() == before + 1
    assert (
        f"late delivery: notification_id={notification_id} webhook_id=wh-1"
        " latency_seconds=901 bound_seconds=900"
    ) in caplog.text
    assert [p["within_bound"] for p in ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")] == [
        True,
        False,
    ]


def test_delivery_attempt_is_logged(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.8: each attempt logs notification id, webhook id, status and duration."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    notification_id = request_notification(app_engine, owner_engine)
    receiver.statuses = [503]
    with caplog.at_level(logging.INFO, logger="worker"):
        notifications.deliver_due(app_engine)
    [line] = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("delivery attempt")
    ]
    assert line.startswith(
        f"delivery attempt: notification_id={notification_id} webhook_id=wh-1 attempt=1"
        " status=503 duration_ms="
    )


def test_health_values(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.8: seconds since the last sweep (real time), pending deliveries, and the age of
    the oldest pending one, for Part 4's health endpoint (decision 97)."""
    assert worker.health_values(app_engine) == worker.HealthValues(None, 0, None)

    wall = datetime(2031, 1, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "wall_now", lambda: wall)
    gaps.sweep(app_engine)  # writes the heartbeat
    add_webhook(owner_engine, "wh-1", closed_port_url(), ["flag_notification"])
    request_notification(app_engine, owner_engine, "evt-a")
    fake_clock.advance(30)
    request_notification(app_engine, owner_engine, "evt-b")
    notifications._route(app_engine)
    fake_clock.advance(15)
    monkeypatch.setattr(clock, "wall_now", lambda: wall + timedelta(seconds=42.7))
    assert worker.health_values(app_engine) == worker.HealthValues(
        sweeper_last_ran_seconds_ago=42,
        pending_deliveries=2,
        oldest_pending_delivery_seconds=45,
    )


# Spec 3.9 rows and exact entries


def test_retries_500_500_200_make_exactly_three_attempts(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.9 'Retries': 500, 500, then 200 gives exactly three attempts at the scheduled
    delays (0, then 10 s later, then 30 s after that: T0, T0+10, T0+40) and one
    NOTIFICATION_DELIVERED."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    notification_id = request_notification(app_engine, owner_engine)
    receiver.statuses = [500, 500, 200]
    receiver.during = fake_clock.now
    for wait in (0, 10, 30):
        if wait:
            fake_clock.advance(wait - 1)
            assert notifications.deliver_due(app_engine) == 0
            fake_clock.advance(1)
        assert notifications.deliver_due(app_engine) == 1
    fake_clock.advance(3600)
    assert notifications.deliver_due(app_engine) == 0
    assert [call.observed for call in receiver.calls] == [
        T0,
        T0 + timedelta(seconds=10),
        T0 + timedelta(seconds=40),
    ]
    assert ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED") == [
        {
            "notification_id": notification_id,
            "webhook_id": "wh-1",
            "attempt": 3,
            "latency_seconds": 40,
            "within_bound": True,
        }
    ]
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED", "NOTIFICATION_DELIVERED"]


def test_ac04_a_slow_receiver_with_a_low_bound_is_not_within_bound(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.9 'AC-04: bound honored': with NOTIFY_MAX_DELAY_SECONDS set low (2), a
    deliberately slow receiver (3 s on the simulated clock) yields within_bound false; a fast
    one stays within the bound."""
    monkeypatch.setenv("NOTIFY_MAX_DELAY_SECONDS", "2")
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine, "evt-fast")
    notifications.deliver_due(app_engine)
    request_notification(app_engine, owner_engine, "evt-slow")
    receiver.during = lambda: fake_clock.advance(3)  # the receiver takes 3 s to answer
    notifications.deliver_due(app_engine)
    payloads = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert [(p["latency_seconds"], p["within_bound"]) for p in payloads] == [(0, True), (3, False)]


def test_the_send_timeout_is_five_seconds(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.5 step 3: an HTTP POST with a 5-second timeout; a timeout is a failure."""
    seen: list[Any] = []

    def fake_post(url: str, **kwargs: Any) -> Any:
        seen.append((url, kwargs))
        raise httpx.ReadTimeout("simulated")

    monkeypatch.setattr(httpx, "post", fake_post)
    add_webhook(owner_engine, "wh-1", "http://198.51.100.7/hook", ["flag_notification"])
    request_notification(app_engine, owner_engine)
    assert notifications.deliver_due(app_engine) == 1
    [(url, kwargs)] = seen
    assert (url, kwargs["timeout"], kwargs["follow_redirects"]) == (
        "http://198.51.100.7/hook",
        5,
        False,
    )
    row = delivery(owner_engine)
    assert (row["status"], row["attempt_count"], row["last_status"]) == ("PENDING", 1, None)


def test_notification_ledger_entries_have_exact_payload_keys(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 2.4 keys: NOTIFICATION_REQUESTED {notification_id, event_id, notification_type};
    NOTIFICATION_DELIVERED {notification_id, webhook_id, attempt, latency_seconds,
    within_bound}; NOTIFICATION_FAILED {notification_id, webhook_id, attempts, last_status}.
    Actor `system`, subject the event id, integers and booleans as JSON scalars."""
    add_webhook(owner_engine, "wh-ok", receiver.url + "?ok", ["flag_notification"])
    add_webhook(owner_engine, "wh-bad", closed_port_url(), ["flag_notification"])
    request_notification(app_engine, owner_engine)
    for delay in [0, 10, 30, 60, 120, 300]:
        fake_clock.advance(delay)
        notifications.deliver_due(app_engine)
    entries = rows(
        owner_engine,
        "SELECT entry_type, actor_id, subject_id, payload FROM ledger_entry ORDER BY seq",
    )
    shapes = {
        e["entry_type"]: (e["actor_id"], e["subject_id"], sorted(e["payload"])) for e in entries
    }
    assert shapes == {
        "NOTIFICATION_REQUESTED": (
            "system",
            "evt-1",
            ["event_id", "notification_id", "notification_type"],
        ),
        "NOTIFICATION_DELIVERED": (
            "system",
            "evt-1",
            ["attempt", "latency_seconds", "notification_id", "webhook_id", "within_bound"],
        ),
        "NOTIFICATION_FAILED": (
            "system",
            "evt-1",
            ["attempts", "last_status", "notification_id", "webhook_id"],
        ),
    }
    delivered = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")[0]
    assert [type(delivered[k]) for k in ("attempt", "latency_seconds", "within_bound")] == [
        int,
        int,
        bool,
    ]
    failed = ledger_payloads(owner_engine, "NOTIFICATION_FAILED")[0]
    assert (failed["webhook_id"], failed["attempts"], failed["last_status"]) == ("wh-bad", 6, None)


def test_worker_resilience_a_crash_between_claim_and_send_is_retried_after_the_lease(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.9 'Worker resilience': a crash between claim and send results in a retry after
    the lease expires, not a lost notification."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine, owner_engine)
    notifications._route(app_engine)
    [claim] = notifications._claim(app_engine)  # the worker crashes here, before sending
    assert receiver.calls == []
    fake_clock.advance(notifications.LEASE_SECONDS - 1)
    assert notifications.deliver_due(app_engine) == 0  # still leased
    fake_clock.advance(1)
    assert notifications.deliver_due(app_engine) == 1
    assert len(receiver.calls) == 1
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert payload["attempt"] == claim.attempt + 1
    assert delivery(owner_engine)["status"] == "DELIVERED"
