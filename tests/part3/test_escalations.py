"""Phase 4 Part 3: planning and firing (spec 3.3, 3.4, 3.9; Addendum rules 3 and 5), with plan and
fire_due called directly and a replaced dispositions.summary (Addendum rule 10). Clock: FakeClock.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import escalations, gaps, ledger, platform, worker
from app.escalations import instant
from app.ledger.verify import verify_ledger
from tests.part3.conftest import ANCHOR, DAY, Disposer, escalation_rows, triggered
from tests.worker.conftest import FakeClock, add_system, rows

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)


def pcts(fired: list[tuple[str, int]]) -> list[int]:
    return [pct for _, pct in fired]


def statuses(owner_engine: Engine, event_id: str = "evt-1") -> dict[int, str]:
    return {row["pct"]: row["status"] for row in escalation_rows(owner_engine, event_id)}


def due_times(owner_engine: Engine, event_id: str = "evt-1") -> dict[int, datetime]:
    return {row["pct"]: row["due_at"] for row in escalation_rows(owner_engine, event_id)}


@pytest.mark.parametrize(
    ("pct", "offset"),
    [
        (49, 7 * DAY + 8 * HOUR + 24 * MINUTE),
        (50, 7 * DAY + 12 * HOUR),
        (79, 11 * DAY + 20 * HOUR + 24 * MINUTE),
        (80, 12 * DAY),
        (99, 14 * DAY + 20 * HOUR + 24 * MINUTE),
        (100, 15 * DAY),
    ],
)
def test_instant_on_a_15_day_window(pct: int, offset: timedelta) -> None:
    assert instant(ANCHOR, ANCHOR + 15 * DAY, pct) - ANCHOR == offset


def test_instant_rounds_up_to_a_whole_microsecond() -> None:
    """Spec 6: ceil, so a reminder is never early."""
    window = timedelta(microseconds=3)
    assert instant(ANCHOR, ANCHOR + window, 50) - ANCHOR == timedelta(microseconds=2)


def test_ac12_fires_at_50_80_100_and_not_at_49_79_99(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """AC-12, spec 3.9: exactly three REMINDER_TRIGGERED entries."""
    disposer.dispose(disposer.event(), "OTHER_SERIOUS")
    steps = [
        (7 * DAY + 8 * HOUR + 24 * MINUTE, []),
        (7 * DAY + 12 * HOUR, [50]),
        (11 * DAY + 20 * HOUR + 24 * MINUTE, []),
        (12 * DAY, [80]),
        (14 * DAY + 20 * HOUR + 24 * MINUTE, []),
        (15 * DAY, [100]),
        (16 * DAY, []),
    ]
    for offset, expected in steps:
        fake_clock.set(ANCHOR + offset)
        assert pcts(escalations.fire_due(app_engine)) == expected, offset
    assert [e["payload"]["escalation_pct"] for e in triggered(owner_engine)] == [50, 80, 100]


@pytest.mark.parametrize("pct", [50, 80, 100])
def test_one_second_early_fires_nothing_and_the_instant_fires(
    app_engine: Engine, fake_clock: FakeClock, disposer: Disposer, pct: int
) -> None:
    disposer.dispose(disposer.event(), "OTHER_SERIOUS")
    fake_clock.set(instant(ANCHOR, ANCHOR + 15 * DAY, pct) - SECOND)
    escalations.fire_due(app_engine)
    before = rows(disposer.owner_engine, "SELECT count(*) AS n FROM ledger_entry")
    assert escalations.fire_due(app_engine) == []
    assert rows(disposer.owner_engine, "SELECT count(*) AS n FROM ledger_entry") == before
    fake_clock.set(instant(ANCHOR, ANCHOR + 15 * DAY, pct))
    assert pcts(escalations.fire_due(app_engine)) == [pct]


def test_a_big_jump_fires_every_percentage_once_in_order(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    disposer.dispose(disposer.event(), "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 20 * DAY)
    assert escalations.fire_due(app_engine) == [("evt-1", 50), ("evt-1", 80), ("evt-1", 100)]
    assert [e["payload"]["escalation_pct"] for e in triggered(owner_engine)] == [50, 80, 100]


def test_a_jump_walks_past_one_batch(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.4 step 7: more due rows than one batch all fire in one call."""
    monkeypatch.setattr(escalations, "BATCH_SIZE", 2)
    for name in ("evt-1", "evt-2"):
        disposer.dispose(disposer.event(name), "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 20 * DAY)
    assert len(escalations.fire_due(app_engine)) == 6
    assert set(statuses(owner_engine, "evt-2").values()) == {"SENT"}


def test_no_duplicates_after_further_passes(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 10 * DAY)
    assert len(escalations.fire_due(app_engine)) == 3
    for _ in range(10):
        fake_clock.advance(3600)
        assert escalations.fire_due(app_engine) == []
    assert len(triggered(owner_engine)) == 3


def test_two_callers_across_the_50_instant_fire_one_reminder_each_time(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Spec 3.9 Concurrency, on the branch: fire_due from two threads, repeated 50 times."""
    fake_clock.set(instant(ANCHOR, ANCHOR + 15 * DAY, 50))
    for round_number in range(50):
        event_id = disposer.event(f"evt-{round_number}")
        disposer.dispose(event_id, "OTHER_SERIOUS")
        first, second = fire_from_two_threads(app_engine)
        assert sorted(first + second) == [(event_id, 50)]
    assert len(triggered(owner_engine)) == 50
    assert ledger_is_valid(owner_engine)


def fire_from_two_threads(
    app_engine: Engine,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Both callers leave a barrier together. An exception in either one fails the test."""
    barrier = threading.Barrier(2)

    def caller() -> list[tuple[str, int]]:
        barrier.wait()
        return escalations.fire_due(app_engine)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(caller), pool.submit(caller)
        return first.result(), second.result()


def ledger_is_valid(owner_engine: Engine) -> bool:
    with Session(owner_engine) as session:
        return bool(verify_ledger(session)["valid"])


def test_model_5_ex_1_a_stricter_revision_before_50(
    app_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Spec 3.9: OTHER_SERIOUS, DEATH on day 3: +5d, +8d, +10d of the first assignment, and
    nothing at +7d 12:00 beyond the 50% already fired at +5d."""
    event_id = disposer.event()
    disposer.dispose(event_id, "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 3 * DAY)
    disposer.dispose(event_id, "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY - SECOND)
    assert escalations.fire_due(app_engine) == []
    fake_clock.set(ANCHOR + 5 * DAY)
    assert pcts(escalations.fire_due(app_engine)) == [50]
    fake_clock.set(ANCHOR + 7 * DAY + 12 * HOUR)
    assert escalations.fire_due(app_engine) == []
    fake_clock.set(ANCHOR + 8 * DAY)
    assert pcts(escalations.fire_due(app_engine)) == [80]
    fake_clock.set(ANCHOR + 10 * DAY)
    assert pcts(escalations.fire_due(app_engine)) == [100]


def test_a_stricter_revision_after_50_sent(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Spec 3.9: 50% sent at +7d 12:00, DEATH at +8d: 50% not resent, 80% at once, 100% at +10d."""
    event_id = disposer.event()
    disposer.dispose(event_id, "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 7 * DAY + 12 * HOUR)
    assert pcts(escalations.fire_due(app_engine)) == [50]
    sent_due = due_times(owner_engine)[50]
    fake_clock.set(ANCHOR + 8 * DAY)
    disposer.dispose(event_id, "DEATH")
    assert due_times(owner_engine)[50] == sent_due  # a SENT row is unchanged
    assert pcts(escalations.fire_due(app_engine)) == [80]
    assert escalations.fire_due(app_engine) == []
    fake_clock.set(ANCHOR + 10 * DAY)
    assert pcts(escalations.fire_due(app_engine)) == [100]
    tiers = [e["payload"]["tier"] for e in triggered(owner_engine)]
    assert tiers == ["OTHER_SERIOUS", "DEATH", "DEATH"]


def test_a_looser_revision_moves_the_reminders_later(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    event_id = disposer.event()
    disposer.dispose(event_id, "DEATH")
    fake_clock.set(ANCHOR + DAY)
    disposer.dispose(event_id, "OTHER_SERIOUS")
    expected = {pct: instant(ANCHOR, ANCHOR + 15 * DAY, pct) for pct in (50, 80, 100)}
    assert due_times(owner_engine) == expected
    fake_clock.set(ANCHOR + 5 * DAY)
    assert escalations.fire_due(app_engine) == []
    fake_clock.set(ANCHOR + 7 * DAY + 12 * HOUR)
    assert pcts(escalations.fire_due(app_engine)) == [50]


def test_a_first_not_serious_has_no_rows_and_a_reopen_inserts_the_set(
    owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Ex-2 and Addendum rule 5: the first plan is the one that finds no rows."""
    event_id = disposer.event()
    disposer.dispose(event_id, "NOT_SERIOUS")
    assert escalation_rows(owner_engine) == []
    fake_clock.set(ANCHOR + DAY)
    disposer.dispose(event_id, "DEATH")
    assert due_times(owner_engine) == {
        pct: instant(ANCHOR, ANCHOR + 10 * DAY, pct) for pct in (50, 80, 100)
    }
    assert set(statuses(owner_engine).values()) == {"PENDING"}


def test_not_serious_cancels_only_unsent_rows_and_a_reopen_does_not_resend(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """D2: 50% sent; NOT_SERIOUS cancels 80 and 100; the reopen re-plans them, 50% stays sent."""
    event_id = disposer.event()
    disposer.dispose(event_id, "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    assert pcts(escalations.fire_due(app_engine)) == [50]
    fake_clock.set(ANCHOR + 6 * DAY)
    disposer.dispose(event_id, "NOT_SERIOUS")
    assert statuses(owner_engine) == {50: "SENT", 80: "CANCELLED", 100: "CANCELLED"}
    [cancelled] = rows(owner_engine, "SELECT cancelled_at FROM escalation WHERE pct = 80")
    assert cancelled["cancelled_at"] == ANCHOR + 6 * DAY

    fake_clock.set(ANCHOR + 20 * DAY)
    assert escalations.fire_due(app_engine) == []  # cancelled rows never fire

    disposer.dispose(event_id, "OTHER_SERIOUS")
    assert statuses(owner_engine) == {50: "SENT", 80: "PENDING", 100: "PENDING"}
    [replanned] = rows(owner_engine, "SELECT cancelled_at FROM escalation WHERE pct = 80")
    assert replanned["cancelled_at"] is None
    # The instants have passed: late, never early.
    assert pcts(escalations.fire_due(app_engine)) == [80, 100]
    assert len(triggered(owner_engine)) == 3


def test_the_configured_set_is_one_reminder_with_50_alone(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.9 Configured set: the phase plan's fallback."""
    monkeypatch.setenv("ESCALATION_PERCENTAGES", "50")
    disposer.dispose(disposer.event(), "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 20 * DAY)
    assert pcts(escalations.fire_due(app_engine)) == [50]
    assert len(triggered(owner_engine)) == 1


def test_the_first_plan_fixes_the_set_for_good(
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.3: a changed setting does not change an event that already has rows."""
    event_id = disposer.event()
    disposer.dispose(event_id, "OTHER_SERIOUS")
    monkeypatch.setenv("ESCALATION_PERCENTAGES", "25")
    disposer.dispose(event_id, "DEATH")
    assert sorted(statuses(owner_engine)) == [50, 80, 100]


def test_anchor_at_never_changes_on_a_replan(
    owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    event_id = disposer.event()
    disposer.dispose(event_id, "OTHER_SERIOUS")
    disposer.dispose(event_id, "DEATH", anchor=ANCHOR)
    anchors = rows(owner_engine, "SELECT DISTINCT anchor_at FROM escalation")
    assert anchors == [{"anchor_at": ANCHOR}]


def test_reminder_triggered_is_the_pinned_entry_and_the_row_keeps_its_values(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Spec 3.4 step 6, Addendum rules 4 and 5."""
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY + 3 * SECOND)
    escalations.fire_due(app_engine)
    [row] = rows(owner_engine, "SELECT * FROM escalation WHERE pct = 50")
    [entry] = triggered(owner_engine)
    assert entry == {
        "actor_id": "system",
        "subject_id": "evt-1",
        "payload": {
            "event_id": "evt-1",
            "escalation_pct": 50,
            "due_at": "2026-10-02T10:12:00Z",
            "tier": "DEATH",
            "anchor_at": "2026-09-27T10:12:00Z",
            "deadline_utc": "2026-10-07T10:12:00Z",
            "notification_id": row["notification_id"],
        },
    }
    assert row["notification_id"].startswith("ntf-")
    assert row["status"] == "SENT"
    assert row["sent_at"] == ANCHOR + 5 * DAY + 3 * SECOND
    assert row["tier"] == "DEATH"
    assert row["anchor_at"] == ANCHOR
    assert row["deadline_utc"] == ANCHOR + 10 * DAY
    [notification] = rows(
        owner_engine,
        "SELECT notification_id, notification_type, escalation_pct FROM notification",
    )
    assert notification == {
        "notification_id": row["notification_id"],
        "notification_type": "reminder",
        "escalation_pct": 50,
    }
    assert ledger_is_valid(owner_engine)


def test_the_due_check_is_repeated_under_the_lock(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    """Spec 3.4 step 2: a row a revision moved after the listing is skipped, never fired early."""
    event_id = disposer.event()
    disposer.dispose(event_id, "OTHER_SERIOUS")
    fake_clock.set(ANCHOR + 5 * DAY)  # listed under DEATH's instants, then moved back
    assert escalations._fire_one(app_engine, event_id, 50) is None
    assert statuses(owner_engine)[50] == "PENDING"
    assert triggered(owner_engine) == []


def test_a_row_held_by_another_caller_is_skipped_and_fires_later(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    with Session(owner_engine) as holder:
        holder.execute(text("SELECT 1 FROM escalation WHERE pct = 50 FOR UPDATE"))
        assert escalations.fire_due(app_engine) == []
        holder.rollback()
    assert pcts(escalations.fire_due(app_engine)) == [50]


def test_a_row_already_sent_or_gone_is_skipped(
    app_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    assert escalations._fire_one(app_engine, "evt-1", 50) is not None
    assert escalations._fire_one(app_engine, "evt-1", 50) is None  # already SENT
    assert escalations._fire_one(app_engine, "evt-1", 77) is None  # no such row
    assert escalations._fire_one(app_engine, "evt-gone", 50) is None  # emptied by a reset


def test_a_rolled_back_attempt_stays_pending_and_does_not_stop_the_others(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.4: nothing is lost. The failing row's update and notification roll back together,
    the pass ends, the other event fires, and the next pass fires the failed row."""
    disposer.dispose(disposer.event("evt-a"), "DEATH")
    disposer.dispose(disposer.event("evt-b"), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY)
    real_append = ledger.append

    def failing(session: Session, entry_type: str, actor: str, subject: str, payload: Any) -> Any:
        if entry_type == "REMINDER_TRIGGERED" and subject == "evt-a":
            raise RuntimeError("secret detail")
        return real_append(session, entry_type, actor, subject, payload)

    monkeypatch.setattr(ledger, "append", failing)
    with caplog.at_level(logging.WARNING, logger="worker"):
        assert escalations.fire_due(app_engine) == [("evt-b", 50)]
    assert "reminder not fired: event_id=evt-a pct=50 type=RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    assert statuses(owner_engine, "evt-a")[50] == "PENDING"
    assert rows(owner_engine, "SELECT 1 FROM notification WHERE event_id = 'evt-a'") == []

    monkeypatch.setattr(ledger, "append", real_append)
    assert escalations.fire_due(app_engine) == [("evt-a", 50)]


def test_a_row_of_an_event_not_dispositioned_is_cancelled(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.4 step 3: cannot happen with correct planning."""
    disposer.dispose(disposer.event(), "DEATH")
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE event SET state = 'CLOSED'"))
    fake_clock.set(ANCHOR + 5 * DAY)
    with caplog.at_level(logging.WARNING, logger="worker"):
        assert escalations.fire_due(app_engine) == []
    assert "reminder cancelled, event not dispositioned" in caplog.text
    assert statuses(owner_engine)[50] == "CANCELLED"
    assert triggered(owner_engine) == []


def test_a_gap_event_never_gets_escalation_rows(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    add_system(owner_engine, "sys-42", last_received_at=fake_clock.current)
    fake_clock.advance(3600)
    assert len(gaps.sweep(app_engine)) == 1
    fake_clock.advance(30 * 86400)
    assert escalations.fire_due(app_engine) == []
    assert rows(owner_engine, "SELECT * FROM escalation") == []


def test_a_reset_while_reminders_are_due_leaves_no_rows_and_the_worker_keeps_running(
    app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock, disposer: Disposer
) -> None:
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 20 * DAY)
    platform.reset()
    assert rows(owner_engine, "SELECT * FROM escalation") == []
    worker.run_iteration(app_engine, sweep=False)
    assert escalations.fire_due(app_engine) == []


def test_every_pass_and_every_reminder_are_logged(
    app_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.8."""
    disposer.dispose(disposer.event(), "DEATH")
    fake_clock.set(ANCHOR + 5 * DAY + 7 * SECOND)
    with caplog.at_level(logging.INFO, logger="worker"):
        escalations.fire_due(app_engine)
    assert (
        "reminder fired: event_id=evt-1 pct=50 due_at=2026-10-02T10:12:00Z late_seconds=7"
        in caplog.text
    )
    assert "escalation pass: rows_due=1 fired=1 skipped=0 failed=0" in caplog.text


def test_a_late_reminder_warns_and_is_counted(
    app_engine: Engine,
    fake_clock: FakeClock,
    disposer: Disposer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 3.8: more than NOTIFY_MAX_DELAY_SECONDS (900) after the instant; exactly 900 is not."""
    disposer.dispose(disposer.event("evt-1"), "DEATH")
    disposer.dispose(disposer.event("evt-2"), "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD")
    before = escalations.late_reminder_count()
    fake_clock.set(ANCHOR + DAY + 900 * SECOND)  # evt-2's 50% exactly at the bound
    with caplog.at_level(logging.WARNING, logger="worker"):
        escalations.fire_due(app_engine)
    assert escalations.late_reminder_count() == before
    assert "late reminder" not in caplog.text
    fake_clock.set(ANCHOR + 5 * DAY + 901 * SECOND)  # evt-1's 50% and evt-2's 80, 100 are late
    with caplog.at_level(logging.WARNING, logger="worker"):
        escalations.fire_due(app_engine)
    assert escalations.late_reminder_count() == before + 3
    assert "late reminder: event_id=evt-1 pct=50 late_seconds=901 bound_seconds=900" in caplog.text
