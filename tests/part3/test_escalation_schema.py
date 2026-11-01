"""Phase 4 Part 3: migration 0007 (spec 3.5, 3.7; Addendum rule 5) and ESCALATION_PERCENTAGES
(spec 8)."""

from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import config, worker
from app.main import create_app
from tests.conftest import alembic_config
from tests.part3.conftest import ANCHOR
from tests.worker.conftest import add_event, add_system, rows

GOOD = {
    "event_id": "evt-1",
    "pct": 50,
    "status": "PENDING",
    "due_at": ANCHOR,
    "anchor_at": ANCHOR,
    "updated_at": ANCHOR,
    "notification_id": None,
    "sent_at": None,
    "cancelled_at": None,
    "tier": None,
    "deadline_utc": None,
}
INSERT = (
    "INSERT INTO escalation (event_id, pct, status, due_at, anchor_at, updated_at,"
    " notification_id, sent_at, cancelled_at, tier, deadline_utc) VALUES (:event_id, :pct,"
    " :status, :due_at, :anchor_at, :updated_at, :notification_id, :sent_at, :cancelled_at,"
    " :tier, :deadline_utc)"
)


@pytest.fixture
def event(owner_engine: Engine) -> str:
    add_system(owner_engine, "sys-42")
    add_event(owner_engine, "evt-1")
    return "evt-1"


def test_a_good_row_inserts(app_engine: Engine, event: str) -> None:
    with app_engine.begin() as conn:
        conn.execute(text(INSERT), GOOD)


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"pct": 0}, "ck_escalation_pct"),
        ({"pct": 101}, "ck_escalation_pct"),
        ({"status": "DONE"}, "ck_escalation_status"),
        ({"status": "SENT"}, "ck_escalation_sent"),
        ({"notification_id": "ntf-1"}, "ck_escalation_sent"),
        ({"status": "CANCELLED"}, "ck_escalation_cancelled"),
        ({"cancelled_at": ANCHOR}, "ck_escalation_cancelled"),
        ({"event_id": "evt-missing"}, "fk_escalation_event"),
    ],
)
def test_constraints(
    app_engine: Engine, event: str, changes: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), app_engine.begin() as conn:
        conn.execute(text(INSERT), {**GOOD, **changes})


def test_one_row_per_event_and_percentage_and_one_per_notification(
    app_engine: Engine, event: str
) -> None:
    sent = {
        **GOOD,
        "status": "SENT",
        "notification_id": "ntf-1",
        "sent_at": ANCHOR,
        "tier": "DEATH",
        "deadline_utc": ANCHOR + timedelta(days=10),
    }
    with app_engine.begin() as conn:
        conn.execute(text(INSERT), sent)
    with pytest.raises(IntegrityError, match="escalation_pkey"), app_engine.begin() as conn:
        conn.execute(text(INSERT), GOOD)
    with (
        pytest.raises(IntegrityError, match="uq_escalation_notification"),
        app_engine.begin() as conn,
    ):
        conn.execute(text(INSERT), {**sent, "pct": 80})


def test_notification_id_has_no_foreign_key(owner_engine: Engine) -> None:
    """Addendum rule 5: spec 3.4 sets it before enqueue inserts the notification."""
    found = rows(
        owner_engine,
        "SELECT conname FROM pg_constraint WHERE conrelid = 'escalation'::regclass"
        " AND contype = 'f'",
    )
    assert found == [{"conname": "fk_escalation_event"}]


def test_the_due_index_is_on_status_and_due_at(owner_engine: Engine) -> None:
    [index] = rows(
        owner_engine,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_escalation_due'",
    )
    assert "(status, due_at)" in index["indexdef"]


def test_notification_escalation_pct_is_set_exactly_on_reminders(
    owner_engine: Engine, event: str
) -> None:
    insert = text(
        "INSERT INTO notification (notification_id, event_id, notification_type,"
        " escalation_pct, created_at) VALUES (:id, 'evt-1', :type, :pct, :t)"
    )
    for kind, pct in (("reminder", None), ("flag_notification", 50)):
        with (
            pytest.raises(IntegrityError, match="ck_notification_escalation_pct"),
            owner_engine.begin() as conn,
        ):
            conn.execute(insert, {"id": "ntf-1", "type": kind, "pct": pct, "t": ANCHOR})


def test_downgrade_removes_reminders_and_restores_the_old_constraint(
    owner_engine: Engine, event: str
) -> None:
    """The reminders cannot live under one-notification-per-event. The ledger is not touched."""
    insert = text(
        "INSERT INTO notification (notification_id, event_id, notification_type,"
        " escalation_pct, created_at) VALUES (:id, 'evt-1', :type, :pct, :t)"
    )
    with owner_engine.begin() as conn:
        conn.execute(insert, {"id": "ntf-f", "type": "flag_notification", "pct": None, "t": ANCHOR})
        conn.execute(insert, {"id": "ntf-r", "type": "reminder", "pct": 50, "t": ANCHOR})
    try:
        command.downgrade(alembic_config(), "0006")
        assert rows(owner_engine, "SELECT notification_id FROM notification") == [
            {"notification_id": "ntf-f"}
        ]
        assert rows(owner_engine, "SELECT to_regclass('escalation') AS t") == [{"t": None}]
        names = rows(
            owner_engine,
            "SELECT conname FROM pg_constraint WHERE conname = 'uq_notification_event'",
        )
        assert names == [{"conname": "uq_notification_event"}]
    finally:
        command.upgrade(alembic_config(), "head")
    assert rows(owner_engine, "SELECT to_regclass('escalation')::text AS t") == [
        {"t": "escalation"}
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (50, 80, 100)),
        ("", (50, 80, 100)),
        ("  ", (50, 80, 100)),
        ("50", (50,)),
        ("1,100", (1, 100)),
        (" 25 , 75 ", (25, 75)),
    ],
)
def test_percentages_accepted(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: tuple[int, ...]
) -> None:
    if raw is None:
        monkeypatch.delenv("ESCALATION_PERCENTAGES", raising=False)
    else:
        monkeypatch.setenv("ESCALATION_PERCENTAGES", raw)
    assert config.escalation_percentages() == expected


@pytest.mark.parametrize(
    "raw",
    ["0", "101", "1000", "-5", "50.5", "fifty", "80,50", "50,50", "50,", ",50", "50,,80", "5 0"],
)
def test_percentages_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("ESCALATION_PERCENTAGES", raw)
    with pytest.raises(RuntimeError, match="ESCALATION_PERCENTAGES"):
        config.escalation_percentages()


def test_a_bad_setting_stops_the_api_and_the_worker_at_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ESCALATION_PERCENTAGES", "80,50")
    with pytest.raises(RuntimeError, match="ESCALATION_PERCENTAGES"):
        create_app()
    with pytest.raises(RuntimeError, match="ESCALATION_PERCENTAGES"):
        worker.main()
