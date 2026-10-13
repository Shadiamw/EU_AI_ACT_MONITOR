"""Part 3: notifications (REQ-04) and the delivery pass.

Deliveries go to a real HTTP receiver on 127.0.0.1. Times are on the fake clock (T0 =
2026-10-01T09:00:00Z); the default bound is 900 seconds and the default attempt limit is 5.
"""

import logging
import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import ledger, notifications
from app.notifications import retry_delay_seconds
from tests.api.conftest import append_lock_is_free
from tests.worker.conftest import (
    T0,
    FakeClock,
    Receiver,
    add_webhook,
    app_sessions_in_transaction,
    closed_port_url,
    entry_types,
    rows,
)


def request_notification(app_engine: Engine, event_id: str = "evt-1") -> str:
    """What the ingest or the sweeper does: enqueue in its transaction, then commit."""
    with Session(app_engine) as session:
        notification_id = notifications.enqueue(session, event_id, "sys-42")
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


def test_enqueue_records_the_request_and_one_delivery_per_subscribed_webhook(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_webhook(owner_engine, "wh-flag", "http://127.0.0.1:9/a", ["flag_notification"])
    add_webhook(owner_engine, "wh-both", "http://127.0.0.1:9/b", ["reminder", "flag_notification"])
    add_webhook(owner_engine, "wh-reminder", "http://127.0.0.1:9/c", ["reminder"])
    notification_id = request_notification(app_engine)

    entries = rows(owner_engine, "SELECT actor_id, subject_id, payload FROM ledger_entry")
    assert entries == [
        {
            "actor_id": "sys-42",
            "subject_id": "evt-1",
            "payload": {
                "notification_id": notification_id,
                "event_id": "evt-1",
                "notification_type": "flag_notification",
            },
        }
    ]
    deliveries = rows(
        owner_engine,
        "SELECT webhook_id, status, attempts, next_attempt_at FROM notification_delivery"
        " ORDER BY webhook_id",
    )
    assert deliveries == [
        {"webhook_id": wid, "status": "PENDING", "attempts": 0, "next_attempt_at": T0}
        for wid in ("wh-both", "wh-flag")
    ]


def test_enqueue_refuses_an_unknown_type_and_writes_nothing(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    with Session(app_engine) as session:
        with pytest.raises(ValueError):
            notifications.enqueue(session, "evt-1", "sys-42", "email")
        session.rollback()
    assert entry_types(owner_engine) == []
    assert append_lock_is_free(owner_engine)


def test_enqueue_does_not_commit(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    with Session(app_engine) as session:
        notifications.enqueue(session, "evt-1", "sys-42")
        session.rollback()
    assert entry_types(owner_engine) == []
    assert rows(owner_engine, "SELECT * FROM notification") == []


# Delivery


def test_delivery_appends_delivered_with_integer_latency_and_boolean_bound(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    notification_id = request_notification(app_engine)
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
    assert call.body == {
        "type": "flag_notification",
        "notification_id": notification_id,
        "webhook_id": "wh-1",
        "event_id": "evt-1",
        "requested_at": "2026-10-01T09:00:00Z",
        "attempt": 1,
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
    assert (row["status"], row["attempts"], row["last_status"]) == ("DELIVERED", 1, 200)
    assert row["latency_seconds"] == 7
    assert row["lease_expires_at"] is None

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
    request_notification(app_engine)
    fake_clock.advance(delay)
    notifications.deliver_due(app_engine)
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert (payload["latency_seconds"], payload["within_bound"]) == (delay, within)


def test_the_bound_is_configurable(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_MAX_DELAY_SECONDS", "60")
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine)
    fake_clock.advance(61)
    notifications.deliver_due(app_engine)
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert payload["within_bound"] is False


def test_each_subscribed_webhook_gets_its_own_delivery(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-a", receiver.url, ["flag_notification"])
    add_webhook(owner_engine, "wh-b", receiver.url, ["flag_notification"])
    request_notification(app_engine)
    assert notifications.deliver_due(app_engine) == 2
    assert sorted(c.body["webhook_id"] for c in receiver.calls) == ["wh-a", "wh-b"]
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


def test_retry_delays_double_from_30_seconds_up_to_240() -> None:
    assert [retry_delay_seconds(n) for n in range(1, 7)] == [30, 60, 120, 240, 240, 240]


@pytest.mark.parametrize("status", [302, 404, 500, 503])
def test_a_non_2xx_answer_schedules_a_retry_and_appends_nothing(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    status: int,
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine)
    receiver.statuses = [status]
    notifications.deliver_due(app_engine)

    row = delivery(owner_engine)
    assert (row["status"], row["attempts"], row["last_status"]) == ("PENDING", 1, status)
    assert row["next_attempt_at"] == T0 + timedelta(seconds=30)
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED"]

    fake_clock.advance(29)
    assert notifications.deliver_due(app_engine) == 0  # not due yet
    fake_clock.advance(1)
    assert notifications.deliver_due(app_engine) == 1
    [payload] = ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")
    assert (payload["attempt"], payload["latency_seconds"]) == (2, 30)
    assert [c.body["attempt"] for c in receiver.calls] == [1, 2]


def test_the_last_failed_attempt_appends_failed_and_stops(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    notification_id = request_notification(app_engine)
    receiver.default_status = 500
    started = []
    for delay in [0, 30, 60, 120, 240]:
        fake_clock.advance(delay)
        started.append(fake_clock.now() - T0)
        assert notifications.deliver_due(app_engine) == 1
    # Five attempts at 0, 30, 90, 210 and 450 s: all inside the 900 s bound.
    assert [int(s.total_seconds()) for s in started] == [0, 30, 90, 210, 450]
    assert ledger_payloads(owner_engine, "NOTIFICATION_FAILED") == [
        {
            "notification_id": notification_id,
            "webhook_id": "wh-1",
            "attempts": 5,
            "last_status": 500,
        }
    ]
    [entry] = rows(
        owner_engine,
        "SELECT actor_id, subject_id FROM ledger_entry WHERE entry_type = 'NOTIFICATION_FAILED'",
    )
    assert entry == {"actor_id": "system", "subject_id": "evt-1"}
    assert delivery(owner_engine)["status"] == "FAILED"
    fake_clock.advance(86400)
    assert notifications.deliver_due(app_engine) == 0
    assert len(receiver.calls) == 5


def test_no_response_is_a_failure_with_a_null_status(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "1")
    add_webhook(owner_engine, "wh-1", closed_port_url(), ["flag_notification"])
    notification_id = request_notification(app_engine)
    assert notifications.deliver_due(app_engine) == 1
    assert ledger_payloads(owner_engine, "NOTIFICATION_FAILED") == [
        {
            "notification_id": notification_id,
            "webhook_id": "wh-1",
            "attempts": 1,
            "last_status": None,
        }
    ]


# Concurrency, leases and lock timeouts


def test_a_claimed_delivery_is_not_taken_by_a_second_pass(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine)
    in_call = threading.Event()
    release = threading.Event()

    def slow_post(url: str, body: dict[str, Any], timeout: int) -> int | None:
        in_call.set()
        assert release.wait(30)
        return 200

    first = threading.Thread(target=notifications.deliver_due, args=(app_engine, slow_post))
    first.start()
    assert in_call.wait(30)
    assert notifications.deliver_due(app_engine) == 0  # IN_FLIGHT, lease still running
    release.set()
    first.join()
    assert len(ledger_payloads(owner_engine, "NOTIFICATION_DELIVERED")) == 1
    assert receiver.calls == []


def test_an_expired_lease_is_claimed_again_and_the_stale_result_is_ignored(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["flag_notification"])
    request_notification(app_engine)
    [stale] = notifications._claim(app_engine)  # a pass that crashed before recording
    assert delivery(owner_engine)["status"] == "IN_FLIGHT"

    fake_clock.advance(notifications.lease_seconds() - 1)
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
    request_notification(app_engine)
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
        assert delivery(owner_engine)["status"] == "IN_FLIGHT"
        assert app_sessions_in_transaction(owner_engine) == 0

        fake_clock.advance(notifications.lease_seconds())
        assert notifications.deliver_due(timeout_engine) == 1
    finally:
        timeout_engine.dispose()
    assert entry_types(owner_engine) == ["NOTIFICATION_REQUESTED", "NOTIFICATION_DELIVERED"]
    assert len(receiver.calls) == 2  # at least once: the receiver saw the notification twice
