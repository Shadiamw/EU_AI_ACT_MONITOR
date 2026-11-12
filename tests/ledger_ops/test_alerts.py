"""Phase 5 Part 3, M7: alerts A1 to A9 and their log lines (spec 7.3; decisions 305, 307-310).

Each alert is checked just below, at and just past its threshold through ops.current_alerts,
then ops.raise_alerts must log it once per code and subject per wall-clock UTC day (ERROR for
critical, WARNING otherwise) and again on the next day. A2 and A3 read the wall clock (patched
as app.clock.wall_now); the others read the app clock (the fake clock). Expected values come
from the spec, not from output.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, notifications
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.ledger_ops.test_projection import PART1_MISSING, breach, dispose, view
from tests.part1.conftest import get, rows
from tests.worker.conftest import FakeClock, add_webhook

WALL = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
# Spec 7.3 severities.
SEVERITY = {
    "A1": "critical",
    "A2": "warning",
    "A3": "critical",
    "A4": "warning",
    "A5": "warning",
    "A6": "critical",
    "A7": "warning",
    "A8": "warning",
    "A9": "info",
}
CODES = list(SEVERITY)


def at_wall(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(clock, "wall_now", lambda: moment)


def alerts(app_engine: Engine, code: str) -> list[dict[str, Any]]:
    """The alerts with this code from ops.current_alerts, as the worker's role reads them."""
    from app import ops

    with Session(app_engine) as session:
        found = [alert for alert in ops.current_alerts(session) if alert["code"] == code]
        session.rollback()
    return found


def insert_run(owner_engine: Engine, run_id: str, at: datetime, **changes: Any) -> None:
    run = {
        "run_id": run_id,
        "trigger": "MANUAL",
        "started_at": at,
        "finished_at": at,
        "valid": True,
        "entries_checked": 1,
        "first_mismatch": None,
        "head_seq": 1,
        "head_hash": "a" * 64,
        "head_check": "OK",
        "projection_mismatches": [],
        "report_mismatches": [],
        **changes,
    }
    run["projection_mismatches"] = json.dumps(run["projection_mismatches"])
    run["report_mismatches"] = json.dumps(run["report_mismatches"])
    values = [f"CAST(:{key} AS jsonb)" if key.endswith("_mismatches") else f":{key}" for key in run]
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO ledger_verification_run ({', '.join(run)})"
                f" VALUES ({', '.join(values)})"
            ),
            run,
        )


def dispositioned(client: ApiClient, fake_clock: FakeClock, app_engine: Engine) -> str:
    event_id = breach(client, fake_clock)
    view(client, event_id, "rev-1")
    assert dispose(client, event_id, "DEATH").status_code == 200
    return event_id


def deadline(app_engine: Engine, event_id: str) -> datetime:
    [row] = rows(app_engine, "SELECT deadline_utc FROM event WHERE event_id = :e", e=event_id)
    value: datetime = row["deadline_utc"]
    return value


# Each check moves a clock to just below, at and just past the threshold and returns the
# subject the alert names (None when it has none); it leaves the alert raised.
Check = Callable[[ApiClient, FakeClock, Engine, Engine, pytest.MonkeyPatch], Any]


def check_a1(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    # A healthy latest run raises nothing; an invalid one, a REGRESSED one, or one with a
    # projection or report mismatch raises A1 (spec 6.1).
    insert_run(owner, "lvr-ok", WALL - timedelta(hours=3))
    assert alerts(app, "A1") == []
    variants: list[dict[str, Any]] = [
        {"valid": False, "first_mismatch": "1"},
        {"head_check": "REGRESSED"},
        {"projection_mismatches": [{"event_id": "evt-x", "column": "event.state"}]},
        {"report_mismatches": [{"report_id": "rpt-x", "check": "json_bytes"}]},
    ]
    for hours, variant in enumerate(variants, start=1):
        run_id = f"lvr-bad-{hours}"
        insert_run(owner, run_id, WALL - timedelta(hours=3) + timedelta(minutes=hours), **variant)
        [alert] = alerts(app, "A1")
        assert alert["detail"]["run_id"] == run_id
    return "lvr-bad-4"


def check_a2(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    # Decision 308: no run at all raises A2 with {"last_run": null}.
    [alert] = alerts(app, "A2")
    assert alert["detail"] == {"last_run": None}
    insert_run(owner, "lvr-a2", WALL)
    for age, raised in ((26 * 3600 - 1, False), (26 * 3600, False), (26 * 3600 + 1, True)):
        at_wall(mp, WALL + timedelta(seconds=age))
        assert len(alerts(app, "A2")) == int(raised), age
    [alert] = alerts(app, "A2")
    assert alert["detail"] == {"last_run": "lvr-a2"}
    return None


def check_a3(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    bound = 3 * config.sweep_interval_seconds()
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM sweeper_heartbeat"))
        conn.execute(text("INSERT INTO sweeper_heartbeat (last_ran_at) VALUES (:t)"), {"t": WALL})
    for age, raised in ((bound - 1, False), (bound, False), (bound + 1, True)):
        at_wall(mp, WALL + timedelta(seconds=age))
        assert len(alerts(app, "A3")) == int(raised), age
    return None


def check_a4(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    add_webhook(owner, "wh-a4", "http://127.0.0.1:9/a4", ["flag_notification"])
    breach(client, fake)
    created = fake.now()
    notifications.deliver_due(app, post=lambda url, body, timeout: 500)  # stays PENDING
    bound = config.notify_max_delay_seconds()
    for age, raised in ((bound - 1, False), (bound, False), (bound + 1, True)):
        fake.set(created + timedelta(seconds=age))
        assert len(alerts(app, "A4")) == int(raised), age
    return None


def check_a5(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    event_id = dispositioned(client, fake, app)
    [row] = rows(
        app,
        "SELECT min(due_at) AS due FROM escalation WHERE event_id = :e AND status = 'PENDING'",
        e=event_id,
    )
    grace = 60  # escalations.OVERDUE_AFTER_SECONDS, the /healthz rule (spec 3.6)
    for after, raised in ((grace - 1, False), (grace, False), (grace + 1, True)):
        fake.set(row["due"] + timedelta(seconds=after))
        assert len(alerts(app, "A5")) == int(raised), after
    return None


def check_a6(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    # Decision 305: no report tables here, so a DISPOSITIONED event counts as unsubmitted.
    event_id = dispositioned(client, fake, app)
    due = deadline(app, event_id)
    for moment, raised in ((due - SECOND, False), (due, False), (due + SECOND, True)):
        fake.set(moment)
        found = alerts(app, "A6")
        assert [alert["detail"] for alert in found] == [{"event_id": event_id}] * raised
    return event_id


def check_a7(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    event_id = dispositioned(client, fake, app)
    due = deadline(app, event_id)
    window = timedelta(hours=24)  # ALERT_DEADLINE_WARNING_HOURS default
    for moment, raised in (
        (due - window - SECOND, False),
        (due - window, False),
        (due - window + SECOND, True),
    ):
        fake.set(moment)
        found = alerts(app, "A7")
        assert [alert["detail"] for alert in found] == [{"event_id": event_id}] * raised
    # Past the deadline it is A6, not A7.
    fake.set(due + SECOND)
    assert alerts(app, "A7") == []
    # The variable moves the window.
    mp.setenv("ALERT_DEADLINE_WARNING_HOURS", "2")
    fake.set(due - timedelta(hours=2))
    assert alerts(app, "A7") == []
    fake.set(due - timedelta(hours=2) + SECOND)
    assert len(alerts(app, "A7")) == 1
    return event_id


def check_a8(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    # Decision 307: the last attempt is the recorded_at of the NOTIFICATION_FAILED entry.
    add_webhook(owner, "wh-a8", "http://127.0.0.1:9/a8", ["flag_notification"])
    breach(client, fake)
    for _ in range(notifications.MAX_ATTEMPTS):
        notifications.deliver_due(app, post=lambda url, body, timeout: 500)
        fake.advance(301)
    [entry] = rows(
        owner,
        "SELECT recorded_at FROM ledger_entry WHERE entry_type = 'NOTIFICATION_FAILED'",
    )
    failed_at = datetime.fromisoformat(entry["recorded_at"])
    window = timedelta(hours=24)
    for moment, raised in (
        (failed_at + window + SECOND, False),
        (failed_at + window, False),
        (failed_at + window - SECOND, True),
    ):
        fake.set(moment)
        found = alerts(app, "A8")
        assert [alert["detail"] for alert in found] == [{"failed_deliveries_24h": 1}] * raised
    return None


def check_a9(client: ApiClient, fake: FakeClock, app: Engine, owner: Engine, mp: Any) -> Any:
    # Decision 305: no event.erased_at here, so every event past retain_until counts.
    event_id = breach(client, fake)
    [row] = rows(app, "SELECT retain_until FROM event WHERE event_id = :e", e=event_id)
    keep = row["retain_until"]
    for moment, raised in ((keep - SECOND, False), (keep, False), (keep + SECOND, True)):
        fake.set(moment)
        found = [alert["detail"] for alert in alerts(app, "A9")]
        assert found == [{"past_retain_until_not_erased": 1}] * raised
    return None


CHECKS: dict[str, Check] = {
    "A1": check_a1,
    "A2": check_a2,
    "A3": check_a3,
    "A4": check_a4,
    "A5": check_a5,
    "A6": check_a6,
    "A7": check_a7,
    "A8": check_a8,
    "A9": check_a9,
}


@pytest.mark.parametrize("code", CODES)
def test_alert_thresholds(
    code: str,
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import ops

    monkeypatch.setattr(ops, "_logged", set())  # a fresh worker's memory (decision 309)
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM sweeper_heartbeat"))
    at_wall(monkeypatch, WALL)
    subject = CHECKS[code](client, fake_clock, app_engine, owner_engine, monkeypatch)

    [alert] = alerts(app_engine, code)
    assert list(alert) == ["code", "severity", "since", "detail"]
    assert alert["severity"] == SEVERITY[code]

    # raise_alerts logs it once per code and subject per wall-clock UTC day, and again the
    # next day (spec 7.3; decision 309).
    def lines() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.getMessage().startswith(f"alert: code={code} ")]

    wall = clock.wall_now()
    with caplog.at_level(logging.INFO):
        ops.raise_alerts(app_engine)
        ops.raise_alerts(app_engine)
    [line] = lines()
    critical = SEVERITY[code] == "critical"
    assert line.levelno == (logging.ERROR if critical else logging.WARNING)
    if subject is not None:
        assert str(subject) in line.getMessage()
    at_wall(monkeypatch, wall + timedelta(days=1))
    with caplog.at_level(logging.INFO):
        ops.raise_alerts(app_engine)
        ops.raise_alerts(app_engine)
    assert len(lines()) == 2


@pytest.mark.integration
def test_alert_deadline_passed_without_submission(
    client: ApiClient, fake_clock: FakeClock, app_engine: Engine
) -> None:
    """Decision 310: A6 is raised once the deadline has passed without a submitted report and
    cleared by a Part 1 submission. Needs Part 1's report assembly and submission (red until
    Part 1 merges)."""
    event_id = dispositioned(client, fake_clock, app_engine)
    fake_clock.set(deadline(app_engine, event_id) + SECOND)
    assert [alert["detail"] for alert in alerts(app_engine, "A6")] == [{"event_id": event_id}]

    url = f"/v1/events/{event_id}/report"
    report = get(client, url, actor="rev-1")
    assert report.status_code == 200, (
        f"{PART1_MISSING}: GET {url} gave {report.status_code} {report.text}"
    )
    submission = client.request(
        "POST",
        f"{url}/submission",
        headers=bearer("rev-1"),
        json={
            "report_id": report.headers["X-Report-Id"],
            "json_sha256": report.headers["X-Report-Sha256"],
            "report_type": "Initial",
        },
    )
    assert submission.status_code == 201, (
        f"{PART1_MISSING}: POST {url}/submission gave {submission.status_code} {submission.text}"
    )
    assert alerts(app_engine, "A6") == []
