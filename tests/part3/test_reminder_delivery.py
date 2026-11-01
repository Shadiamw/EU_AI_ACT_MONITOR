"""Phase 4 Part 3: the outbox changes, the reminder body, the worker loop and the health value
(spec 3.5, 3.6, 3.9; Addendum rule 5). Clock: FakeClock. The receiver is a real HTTP server.
"""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import escalations, gaps, notifications, worker
from tests.part3.conftest import ANCHOR, DAY, Disposer, reminders
from tests.worker.conftest import FakeClock, Receiver, add_webhook, closed_port_url, rows

SECOND = timedelta(seconds=1)


def fire_and_deliver(app_engine: Engine) -> None:
    escalations.fire_due(app_engine)
    notifications.deliver_due(app_engine)


def test_the_reminder_body_is_the_pinned_shape(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    """Spec 3.5, the worked body: exactly these fields."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["reminder"])
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY + 3 * SECOND)
    fire_and_deliver(app_engine)
    [row] = rows(owner_engine, "SELECT notification_id FROM escalation WHERE pct = 50")
    assert [call.body for call in receiver.calls] == [
        {
            "notification_type": "reminder",
            "notification_id": row["notification_id"],
            "event_id": "evt-1",
            "system_id": "sys-42",
            "type": "threshold_breach",
            "detected_at": "2026-09-27T10:12:00Z",
            "tier": "DEATH",
            "assigned_at": "2026-09-27T10:12:00Z",
            "deadline_utc": "2026-10-07T10:12:00Z",
            "escalation_pct": 50,
            "due_at": "2026-10-02T10:12:00Z",
            "sent_at": "2026-10-02T10:12:03Z",
        }
    ]


def test_a_big_jump_reaches_the_receiver_in_order_each_once(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    """Spec 3.9 Big jump. On one fake instant the created_at values tie, so the order rests on
    escalation_pct."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["reminder"])
    disposer.dispose(disposer.event(), "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 20 * DAY)
    fire_and_deliver(app_engine)
    bodies = reminders(receiver.calls)
    assert [body["escalation_pct"] for body in bodies] == [50, 80, 100]
    assert len({body["notification_id"] for body in bodies}) == 3


def test_routing_keeps_reminders_and_flags_apart(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    """Spec 3.9 Routing: flag only gets no reminder; reminder only gets no flag."""
    add_webhook(owner_engine, "wh-flag", f"{receiver.url}/flag", ["flag_notification"])
    add_webhook(owner_engine, "wh-reminder", f"{receiver.url}/reminder", ["reminder"])
    event_id = disposer.event()
    with Session(app_engine) as session:
        notifications.enqueue(session, event_id)
        session.commit()
    disposer.dispose(event_id, "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    fire_and_deliver(app_engine)
    seen = sorted((call.path, call.body["notification_type"]) for call in receiver.calls)
    assert seen == [("/hook/flag", "flag_notification"), ("/hook/reminder", "reminder")]


def test_a_receiver_failing_twice_gets_it_on_the_third_attempt_with_latency_from_due_at(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    """Spec 3.9 Delivery: the event was received days before, so a latency from received_at
    would be far larger than the 45 seconds from due_at (5 + 10 + 30)."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["reminder"])
    receiver.statuses.extend([500, 500])
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY + 5 * SECOND)
    fire_and_deliver(app_engine)
    fake_clock.advance(10)
    notifications.deliver_due(app_engine)
    fake_clock.advance(30)
    notifications.deliver_due(app_engine)
    assert len(receiver.calls) == 3
    assert len({call.body["notification_id"] for call in receiver.calls}) == 1
    [entry] = rows(
        owner_engine,
        "SELECT payload FROM ledger_entry WHERE entry_type = 'NOTIFICATION_DELIVERED'",
    )
    assert entry["payload"]["attempt"] == 3
    assert entry["payload"]["latency_seconds"] == 45
    assert entry["payload"]["within_bound"] is True


def test_a_retry_after_a_revision_keeps_the_body_of_the_trigger(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    """Addendum rule 5: built from the row, so tier, deadline and due_at do not change."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["reminder"])
    receiver.statuses.append(500)
    event_id = disposer.event()
    disposer.dispose(event_id, "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 7 * DAY + 12 * 3600 * SECOND)
    fire_and_deliver(app_engine)
    disposer.dispose(event_id, "DEATH")
    fake_clock.advance(10)
    notifications.deliver_due(app_engine)
    first, second = (call.body for call in receiver.calls if call.body["escalation_pct"] == 50)
    for key in ("tier", "deadline_utc", "due_at", "assigned_at", "notification_id"):
        assert first[key] == second[key]
    assert second["tier"] == "OTHER_SERIOUS"
    assert second["deadline_utc"] == "2026-10-12T10:12:00Z"


def test_a_reminder_already_triggered_is_delivered_after_not_serious(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    """Addendum rule 5, a known limitation: the outbox has no cancel status."""
    add_webhook(owner_engine, "wh-1", receiver.url, ["reminder"])
    event_id = disposer.event()
    disposer.dispose(event_id, "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    escalations.fire_due(app_engine)
    disposer.dispose(event_id, "NOT_SERIOUS")
    notifications.deliver_due(app_engine)
    [body] = reminders(receiver.calls)
    assert body["tier"] == "DEATH"


def test_a_reminder_with_no_escalation_row_waits(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Part 3 decision 203: its body comes from the row, so it is never claimed without one."""
    add_webhook(owner_engine, "wh-1", closed_port_url(), ["reminder"])
    event_id = disposer.event()
    with Session(app_engine) as session:
        notifications.enqueue(session, event_id, "reminder", escalation_pct=50)
        session.commit()
    assert notifications.deliver_due(app_engine) == 0


@pytest.mark.parametrize(
    ("notification_type", "pct"),
    [("reminder", None), ("flag_notification", 50)],
    ids=["reminder-without-pct", "flag-with-pct"],
)
def test_enqueue_refuses_a_wrong_escalation_pct_before_writing(
    app_engine: Engine,
    owner_engine: Engine,
    disposer: Disposer,
    notification_type: str,
    pct: int | None,
) -> None:
    event_id = disposer.event()
    with Session(app_engine) as session, pytest.raises(ValueError, match="escalation_pct"):
        notifications.enqueue(session, event_id, notification_type, escalation_pct=pct)
    assert rows(owner_engine, "SELECT * FROM notification") == []
    assert rows(owner_engine, "SELECT * FROM ledger_entry") == []


def test_enqueue_uses_a_given_id_and_keeps_the_phase_3_payload(
    app_engine: Engine, owner_engine: Engine, disposer: Disposer
) -> None:
    event_id = disposer.event()
    with Session(app_engine) as session:
        returned = notifications.enqueue(
            session, event_id, "reminder", escalation_pct=80, notification_id="ntf-given"
        )
        session.commit()
    assert returned == "ntf-given"
    [entry] = rows(owner_engine, "SELECT payload FROM ledger_entry")
    assert entry["payload"] == {
        "notification_id": "ntf-given",
        "event_id": event_id,
        "notification_type": "reminder",
    }


def test_one_flag_per_event_and_one_reminder_per_percentage(
    app_engine: Engine, disposer: Disposer
) -> None:
    """Spec 3.5, the partial unique indexes."""
    event_id = disposer.event()

    def enqueue(*args: object, **kwargs: object) -> None:
        with Session(app_engine) as session:
            notifications.enqueue(session, event_id, *args, **kwargs)  # type: ignore[arg-type]
            session.commit()

    enqueue()
    enqueue("reminder", escalation_pct=50)
    enqueue("reminder", escalation_pct=80)
    with pytest.raises(IntegrityError, match="uq_notification_flag"):
        enqueue()
    with pytest.raises(IntegrityError, match="uq_notification_reminder"):
        enqueue("reminder", escalation_pct=50)


def test_the_worker_runs_the_sweep_then_reminders_then_deliveries(
    app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 3.6."""
    order: list[str] = []
    monkeypatch.setattr(gaps, "sweep", lambda _: order.append("sweep"))
    monkeypatch.setattr(escalations, "fire_due", lambda _: order.append("fire"))
    monkeypatch.setattr(notifications, "deliver_due", lambda _: order.append("deliver"))
    worker.run_iteration(app_engine, sweep=True)
    assert order == ["sweep", "fire", "deliver"]
    order.clear()
    worker.run_iteration(app_engine, sweep=False)
    assert order == ["fire", "deliver"]


def test_a_reminder_goes_out_in_the_iteration_that_triggers_it(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    receiver: Receiver,
) -> None:
    add_webhook(owner_engine, "wh-1", receiver.url, ["reminder"])
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    worker.run_iteration(app_engine, sweep=False)
    assert [body["escalation_pct"] for body in reminders(receiver.calls)] == [50]


def test_an_error_in_the_reminder_pass_is_logged_and_the_iteration_continues(
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.6: by type name only."""
    delivered: list[bool] = []

    def boom(_: Engine) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(escalations, "fire_due", boom)
    monkeypatch.setattr(notifications, "deliver_due", lambda _: delivered.append(True))
    with caplog.at_level(logging.ERROR, logger="worker"):
        worker.run_iteration(app_engine, sweep=False)
    assert delivered == [True]
    assert "reminder pass failed: type=RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


def test_overdue_escalations_counts_pending_rows_more_than_60_seconds_past_due(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Spec 3.6: exactly 60 s is not overdue; SENT and CANCELLED rows never count."""
    disposer.dispose(disposer.event(), "DEATH")
    assert worker.health_values(app_engine).overdue_escalations == 0
    fake_clock.set(ANCHOR + 5 * DAY + 60 * SECOND)
    assert worker.health_values(app_engine).overdue_escalations == 0
    fake_clock.set(ANCHOR + 5 * DAY + 61 * SECOND)
    assert worker.health_values(app_engine).overdue_escalations == 1
    fake_clock.set(ANCHOR + 20 * DAY)
    assert worker.health_values(app_engine).overdue_escalations == 3
    escalations.fire_due(app_engine)
    assert worker.health_values(app_engine).overdue_escalations == 0

    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE event SET state = 'DISPOSITIONED'"))
    disposer.dispose(disposer.event("evt-2"), "DEATH")
    disposer.dispose("evt-2", "NOT_SERIOUS")
    assert worker.health_values(app_engine).overdue_escalations == 0
