"""Phase 5 Part 3, M10: tests added after the independent audit (audit F-1, F-2, F-5 to F-8, F-10).

Nine test functions, each approved beyond the 18 spec tests. Data is written through the real
app (real keys, the fake clock, PostgreSQL 16); owner edits disable the edited table's user
triggers for one transaction only. Expected values are reasoned out from the spec and the
audit's findings in comments; they are not copied from output.

Most of these tests characterise behaviour that was already correct; their evidence is the
audit's surviving mutants, which they now catch (see the M10 report). Test 9 (F-1) and, on
Part 3 alone, test 2 (it needs Part 1's report table) are red until their fixes are present.
"""

import importlib.util
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

import pytest
from psycopg import errors
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import clock, notifications, worker
from app.formats import api_time
from tests.api.conftest import ApiClient
from tests.db_helpers import HARM_FIELDS, bearer, trigger_disabled
from tests.ledger_ops.test_alerts import alerts, deadline, dispositioned, insert_run
from tests.ledger_ops.test_anchor import HASH_3, head_line, run_script
from tests.ledger_ops.test_projection import (
    DECIDED,
    PART1_MISSING,
    breach,
    build_history,
    projection,
    view,
)
from tests.part1.conftest import get, rows
from tests.script_helpers import FIXTURE
from tests.worker.conftest import FakeClock

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "migrations" / "versions"
SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
LATEST = "(SELECT max(seq_in_event) FROM disposition WHERE event_id = :e)"


def owner_edit(owner_engine: Engine, table: str, sql: str, **params: Any) -> int:
    """One owner transaction: disable the table's user triggers, edit, re-enable."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        changed = conn.execute(text(sql), params).rowcount
        conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    return int(changed)


def dispositions(owner_engine: Engine, event_id: str) -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT tier, deadline_utc, anchor_at, calendar FROM disposition WHERE event_id = :e"
        " ORDER BY seq_in_event",
        e=event_id,
    )


def mismatch(event_id: str, column: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"event_id": event_id, "column": column, "expected": expected, "actual": actual}


# Test 1 (F-2). Each case edits one column of one row as the owner and returns the exact
# mismatches spec 6.3 says the projection check must report. The history is build_history's:
# breach A (accuracy_drift 0.34 > 0.30 on sys-42) dispositioned OTHER_SERIOUS then revised to
# DEATH, with the DECIDED harm fields; gap G2 left open.
Edit = Callable[[Engine, dict[str, Any]], list[dict[str, Any]]]


def edit_event(column: str, value: str, subject: str, before: str, after: str) -> Edit:
    def edit(owner: Engine, history: dict[str, Any]) -> list[dict[str, Any]]:
        event_id = history[subject]
        sql = f"UPDATE event SET {column} = {value} WHERE event_id = :e"
        assert owner_edit(owner, "event", sql, e=event_id) == 1
        return [mismatch(event_id, f"event.{column}", before, after)]

    return edit


def edit_latest_disposition(column: str, value: str) -> Edit:
    def edit(owner: Engine, history: dict[str, Any]) -> list[dict[str, Any]]:
        event_id = history["a"]
        before = dispositions(owner, event_id)[-1][column]
        sql = (
            f"UPDATE disposition SET {column} = {value}"
            f" WHERE event_id = :e AND seq_in_event = {LATEST}"
        )
        assert owner_edit(owner, "disposition", sql, e=event_id) == 1
        after = dispositions(owner, event_id)[-1][column]
        if isinstance(before, datetime):
            before, after = api_time(before), api_time(after)
        assert before != after
        return [mismatch(event_id, f"disposition.{column}", before, after)]

    return edit


def delete_latest_disposition(owner: Engine, history: dict[str, Any]) -> list[dict[str, Any]]:
    # The ledger holds two DISPOSITION_* entries for A; with the revision row gone the count
    # is 1, and the remaining (first) row is compared with the latest payload, so every column
    # where the first row differs from the revision shows too, in spec 6.3 order.
    event_id = history["a"]
    first, latest = dispositions(owner, event_id)
    sql = f"DELETE FROM disposition WHERE event_id = :e AND seq_in_event = {LATEST}"
    assert owner_edit(owner, "disposition", sql, e=event_id) == 1
    expected = [mismatch(event_id, "disposition.count", 2, 1)]
    for column in ("tier", "deadline_utc", "anchor_at", "calendar"):
        want, have = latest[column], first[column]
        if isinstance(want, datetime):
            want, have = api_time(want), api_time(have)
        if want != have:
            expected.append(mismatch(event_id, f"disposition.{column}", want, have))
    assert len(expected) > 1  # OTHER_SERIOUS and DEATH differ at least in tier
    return expected


def edit_harm_field(key: str) -> Edit:
    def edit(owner: Engine, history: dict[str, Any]) -> list[dict[str, Any]]:
        event_id = history["a"]
        before = DECIDED[key]["status"]
        after = "CONFIRMED" if before == "NOT_APPLICABLE" else "NOT_APPLICABLE"
        sql = "UPDATE harm_field SET status = :s WHERE event_id = :e AND field_key = :k"
        assert owner_edit(owner, "harm_field", sql, s=after, e=event_id, k=key) == 1
        return [mismatch(event_id, f"harm_field[{key}].status", before, after)]

    return edit


def no_edit(owner: Engine, history: dict[str, Any]) -> list[dict[str, Any]]:
    return []


EDITS: dict[str, Edit] = {
    "event.system_id": edit_event("system_id", "'sys-99'", "a", "sys-42", "sys-99"),
    "event.metric_name": edit_event("metric_name", "'other'", "a", "accuracy_drift", "other"),
    "event.threshold_value": edit_event("threshold_value", "0.31", "a", "0.30", "0.31"),
    "event.observed_value": edit_event("observed_value", "0.35", "a", "0.34", "0.35"),
    "gap event.system_id": edit_event("system_id", "'sys-99'", "gap_open", "sys-42", "sys-99"),
    "disposition.tier": edit_latest_disposition("tier", "'OTHER_SERIOUS'"),
    "disposition.deadline_utc": edit_latest_disposition(
        "deadline_utc", "deadline_utc + interval '1 day'"
    ),
    "disposition.anchor_at": edit_latest_disposition("anchor_at", "anchor_at + interval '1 hour'"),
    "disposition.calendar": edit_latest_disposition("calendar", "'working_days'"),
    "disposition.count": delete_latest_disposition,
    **{f"harm_field[{key}].status": edit_harm_field(key) for key in HARM_FIELDS},
    "clean": no_edit,
}


@pytest.mark.parametrize("case", list(EDITS))
def test_projection_check_finds_each_edited_column(
    case: str,
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
) -> None:
    history = build_history(client, fake_clock, app_engine)
    assert projection(app_engine) == []
    expected = EDITS[case](owner_engine, history)
    if case == "disposition.deadline_utc":
        # The latest payload carries the deadline the API returned.
        assert expected[0]["expected"] == history["deadline_a"]
    assert projection(app_engine) == expected


@pytest.mark.integration
def test_projection_check_finds_an_edited_report_hash(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
) -> None:
    """Needs Part 1's report table (red on Part 3 alone, like the other integration tests)."""
    history = build_history(client, fake_clock, app_engine)
    event_a = history["a"]
    url = f"/v1/events/{event_a}/report"
    report = get(client, url, actor="rev-1")
    assert report.status_code == 200, (
        f"{PART1_MISSING}: GET {url} gave {report.status_code} {report.text}"
    )
    report_id, json_sha256 = report.headers["X-Report-Id"], report.headers["X-Report-Sha256"]
    assert projection(app_engine) == []

    sql = "UPDATE report SET json_sha256 = :h WHERE report_id = :r"
    assert owner_edit(owner_engine, "report", sql, h="0" * 64, r=report_id) == 1

    # The REPORT_ASSEMBLED payload holds the hash the API returned; the row now holds zeros.
    assert projection(app_engine) == [
        mismatch(event_a, f"report[{report_id}].json_sha256", json_sha256, "0" * 64)
    ]


def test_nightly_and_alert_step_failures_are_logged(
    seeded: None,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import ledger_ops, ops

    calls: list[str] = []
    deliver_due = notifications.deliver_due

    def deliveries(engine: Engine, **kwargs: Any) -> Any:
        calls.append("deliveries")
        return deliver_due(engine, **kwargs)

    def failing_run(engine: Engine, trigger: str) -> None:
        calls.append(f"nightly {trigger}")
        raise ValueError("payload value 0.34 of sys-42")

    def failing_alerts(engine: Engine) -> None:
        calls.append("alerts")
        raise RuntimeError("detail evt-secret")

    monkeypatch.setattr(notifications, "deliver_due", deliveries)
    monkeypatch.setattr(ledger_ops, "nightly_due", lambda engine: True)
    monkeypatch.setattr(ledger_ops, "run_verification", failing_run)
    monkeypatch.setattr(ops, "raise_alerts", failing_alerts)

    with caplog.at_level(logging.INFO):
        worker.run_iteration(app_engine, sweep=False)

    # Deliveries ran, the nightly step failed, and the alert step still ran after it.
    assert calls == ["deliveries", "nightly NIGHTLY", "alerts"]
    # One ERROR line per failed step, by type name only: no message, value or traceback.
    errors_logged = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [(r.name, r.getMessage()) for r in errors_logged] == [
        ("worker", "nightly verification failed: type=ValueError"),
        ("worker", "alert step failed: type=RuntimeError"),
    ]
    assert all(record.exc_info is None for record in errors_logged)
    for value in ("0.34", "sys-42", "evt-secret", "payload", "detail"):
        assert all(value not in record.getMessage() for record in caplog.records)


def test_dashboard_shows_counts_only_after_a_failed_run(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine
) -> None:
    event_id = breach(client, fake_clock)  # accuracy_drift 0.34 on sys-42
    view(client, event_id, "rev-1")
    [k] = [
        row["seq"]
        for row in rows(owner_engine, "SELECT seq, entry_type FROM ledger_entry")
        if row["entry_type"] == "EVENT_FLAGGED"
    ]
    # Model 5 Ex-5: the payload edit ("0.34" -> "0.35") at the EVENT_FLAGGED entry.
    with trigger_disabled(owner_engine) as conn:
        conn.execute(
            text(
                "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}',"
                " '\"0.35\"') WHERE seq = :k"
            ),
            {"k": k},
        )
    response = client.request("POST", "/v1/ledger/verifications", headers=bearer("rev-1"))
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["valid"] is False
    assert len(run["projection_mismatches"]) == 1  # the REVIEWER route shows the run itself

    dashboard = get(client, "/v1/ops/dashboard", actor="viewer-1")
    assert dashboard.status_code == 200, dashboard.text
    summary = dashboard.json()["ledger"]["last_verification"]
    # Spec 7.2: ids and counts. The two lists are counts, as integers (not booleans or lists).
    assert type(summary["projection_mismatches"]) is int
    assert type(summary["report_mismatches"]) is int
    assert (summary["projection_mismatches"], summary["report_mismatches"]) == (1, 0)
    assert "head_hash" not in summary

    health = client.get("/healthz")
    for body in (dashboard.text, health.text):
        for value in ("0.34", "0.35", '"expected"', '"actual"', "head_hash", "accuracy_drift"):
            assert value not in body
        assert re.search(r"[0-9a-f]{64}", body) is None  # no hash of any kind


def test_alert_exact_boundaries(
    client: ApiClient, fake_clock: FakeClock, app_engine: Engine
) -> None:
    from app import ops

    window = timedelta(hours=ops.DUE_SOON.total_seconds() / 3600)
    assert window == timedelta(hours=24)  # spec 7.2 and the ALERT_DEADLINE_WARNING_HOURS default
    event_id = dispositioned(client, fake_clock, app_engine)
    due = deadline(app_engine, event_id)

    def at(moment: datetime) -> tuple[bool, bool, int, int]:
        fake_clock.set(moment)
        body = get(client, "/v1/ops/dashboard", actor="viewer-1").json()["deadlines"]
        return (
            bool(alerts(app_engine, "A7")),
            bool(alerts(app_engine, "A6")),
            body["due_within_24h_unsubmitted"],
            body["passed_unsubmitted"],
        )

    # (A7, A6, due_within_24h_unsubmitted, passed_unsubmitted). Spec 7.3: A7 while
    # deadline - now < 24 h and the deadline has not passed, A6 once now > deadline; spec 7.2:
    # due within 24 h is now <= deadline < now + 24 h.
    assert at(due - window - SECOND) == (False, False, 0, 0)
    assert at(due - window) == (False, False, 0, 0)  # exactly 24 h before: not yet
    assert at(due - window + SECOND) == (True, False, 1, 0)
    assert at(due - SECOND) == (True, False, 1, 0)
    assert at(due) == (True, False, 1, 0)  # exactly at the deadline: A7, not A6
    assert at(due + SECOND) == (False, True, 0, 1)


def test_nightly_unique_index_rejects_a_second_run_on_one_utc_date(
    seeded: None, owner_engine: Engine
) -> None:
    # 2026-11-13T00:30+01:00 is 2026-11-12T23:30Z: the same UTC date as the first run although
    # its local date is the 13th (decision 302: one NIGHTLY run per UTC date of started_at).
    first = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
    second = datetime.fromisoformat("2026-11-13T00:30:00+01:00")
    assert second.astimezone(UTC).date() == first.date()
    insert_run(owner_engine, "lvr-n1", first, trigger="NIGHTLY")
    with pytest.raises(IntegrityError) as raised:
        insert_run(owner_engine, "lvr-n2", second, trigger="NIGHTLY")
    assert isinstance(raised.value.orig, errors.UniqueViolation)
    diag = raised.value.orig.diag
    assert diag.constraint_name == "uq_ledger_verification_run_one_nightly_per_utc_date"

    # MANUAL runs are not limited, and a NIGHTLY run on the next UTC date is accepted.
    insert_run(owner_engine, "lvr-m1", second, trigger="MANUAL")
    insert_run(owner_engine, "lvr-n3", first + DAY, trigger="NIGHTLY")
    ids = rows(owner_engine, "SELECT run_id FROM ledger_verification_run ORDER BY run_id")
    assert [row["run_id"] for row in ids] == ["lvr-m1", "lvr-n1", "lvr-n3"]


HEAD = json.dumps(head_line(3, HASH_3))
NO_HEAD = "anchor: the heads file has no head line (start a new series by hand)\n"
NEW_SERIES = json.dumps(
    {
        "new_series": True,
        "reason": "restart",
        "reset_count": 0,
        "previous_seq": None,
        "previous_hash": None,
        "actor": "operator",
    }
)
# Decision 299: a heads file that cannot be used gives exit 2, nothing on stdout, and a reason
# on stderr. The malformed and non-object lines follow a usable head line, so skipping them
# (audit mutant M49) would anchor to that head instead of failing.
HEADS_FILES: dict[str, tuple[str | None, str]] = {
    "malformed JSON line": (
        f"{HEAD}\n{{not json\n",
        "anchor: line 2 of the heads file is not valid JSON\n",
    ),
    "non-object line": (
        f"{HEAD}\n[3]\n",
        "anchor: line 2 of the heads file is not a JSON object\n",
    ),
    "bad seq": (
        json.dumps({**head_line(3, HASH_3), "seq": "3"}) + "\n",
        "anchor: line 1 of the heads file has no usable seq\n",
    ),
    "bad hash": (
        json.dumps(head_line(3, HASH_3.upper())) + "\n",
        "anchor: line 1 of the heads file has no usable hash\n",
    ),
    "unreadable path": (None, "anchor: cannot read the heads file (No such file or directory)\n"),
    "only a new_series line": (f"{NEW_SERIES}\n", NO_HEAD),
    "empty file": ("", NO_HEAD),
}


@pytest.mark.parametrize("case", list(HEADS_FILES))
def test_anchor_heads_file_errors_exit_2(case: str, tmp_path: Path) -> None:
    content, reason = HEADS_FILES[case]
    heads = tmp_path / "heads.jsonl"
    if content is not None:
        heads.write_text(content, encoding="utf-8")
    # The fixture export is valid and holds seq 3 with HASH_3, so only the heads file fails.
    assert run_script([str(FIXTURE)]).code == 0
    run = run_script([str(FIXTURE), "--anchor", str(heads)])
    assert (run.code, run.stdout, run.stderr) == (2, "", reason)


def test_head_check_after_an_empty_ledger_run_is_ok(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import ledger_ops

    wall = [datetime(2026, 11, 12, 3, 0, tzinfo=UTC)]
    monkeypatch.setattr(clock, "wall_now", lambda: wall[0])
    # An empty ledger (audit F-8): the seed entry is removed as the owner, trigger disabled.
    with trigger_disabled(owner_engine) as conn:
        conn.execute(text("DELETE FROM ledger_entry"))
    assert rows(owner_engine, "SELECT count(*) AS n FROM ledger_entry") == [{"n": 0}]

    # No head, and FIRST_RUN because no run exists yet.
    empty = ledger_ops.run_verification(app_engine, ledger_ops.MANUAL)
    assert empty is not None
    assert (empty["head_seq"], empty["head_hash"], empty["head_check"]) == (None, None, "FIRST_RUN")

    # Entries are added; the previous run saw no head, so nothing can have regressed: OK.
    view(client, breach(client, fake_clock), "rev-1")
    wall[0] += HOUR
    later = ledger_ops.run_verification(app_engine, ledger_ops.MANUAL)
    assert later is not None
    assert later["head_seq"] is not None
    assert later["head_check"] == "OK"
    for run in (empty, later):
        assert run["finished_at"] >= run["started_at"]

    # finished_at is read after the checks: a check that takes five seconds shows in it.
    checks = ledger_ops._checks

    def slow_checks(engine: Engine) -> dict[str, Any]:
        result = checks(engine)
        wall[0] += 5 * SECOND
        return result

    monkeypatch.setattr(ledger_ops, "_checks", slow_checks)
    wall[0] += HOUR
    started = wall[0]
    slow = ledger_ops.run_verification(app_engine, ledger_ops.MANUAL)
    assert slow is not None
    assert (slow["started_at"], slow["finished_at"]) == (started, started + 5 * SECOND)
    [stored] = rows(
        app_engine,
        "SELECT started_at, finished_at FROM ledger_verification_run WHERE run_id = :r",
        r=slow["run_id"],
    )
    assert (stored["started_at"], stored["finished_at"]) == (started, started + 5 * SECOND)


def load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"m10_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NoSql:
    """Stands in for alembic's op: any use of the database fails the test."""

    def get_bind(self) -> Any:
        raise AssertionError("SQL ran before the role name was checked")

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SQL ran before the role name was checked")


@pytest.mark.parametrize(
    "role",
    ['monitor_app"; SELECT 1; --', "monitor_app x", "monitor_APP", "Monitor_app", "a" * 64],
)
def test_0010_role_name_is_validated_strictly(role: str, monkeypatch: pytest.MonkeyPatch) -> None:
    migration = load_migration(VERSIONS / "0010_ledger_ops.py")
    monkeypatch.setattr(migration, "op", NoSql())
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql+psycopg://{quote(role, safe='')}:pw@127.0.0.1:5432/monitor"
    )
    # The whole name must be a plain lowercase role name of at most 63 characters (PostgreSQL's
    # identifier limit), the same rule and error as migrations 0001 to 0007.
    with pytest.raises(RuntimeError) as raised:
        migration._app_role()
    assert str(raised.value) == f"DATABASE_URL user {role!r} is not a plain lowercase role name"

    # Every migration that checks a role name anchors its pattern at both ends, or matches the
    # whole name with fullmatch.
    sources = {path.name: path.read_text() for path in sorted(VERSIONS.glob("*.py"))}
    patterns = {
        name: match.group(1)
        for name, source in sources.items()
        if (match := re.search(r'_ROLE_NAME = re\.compile\(r"([^"]*)"\)', source))
    }
    assert "0010_ledger_ops.py" in patterns
    unanchored = {
        name: pattern
        for name, pattern in patterns.items()
        if not (pattern.startswith("^") and pattern.endswith("$"))
        and "_ROLE_NAME.fullmatch(" not in sources[name]
    }
    assert unanchored == {}
