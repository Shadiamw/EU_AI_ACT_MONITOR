"""Phase 5 Part 3, M4: the nightly verification and alert A1 (spec 6.1, 7.3, 8.6 Ex-5, D11;
decisions 302 and 303).

The ledger is written through the real app (real keys, the fake clock, PostgreSQL 16). The wall
clock, which alone decides when the nightly run is due, is patched as app.clock.wall_now. The
nightly step is driven through worker.run_iteration, as the worker loop runs it. Tampering is
done as the owner role with the trigger disabled for one transaction (Addendum rule 1).
Expected values are reasoned out from the spec in comments; they are not copied from output.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import clock, worker
from app.ledger.canonical import canonical_json, compute_hash
from tests.api.conftest import ApiClient
from tests.db_helpers import trigger_disabled
from tests.ledger_ops.test_projection import PART1_MISSING, breach, dispose, view
from tests.part1.conftest import get, rows
from tests.worker.conftest import FakeClock

# Wall-clock moments (never the simulated clock): the default hour is 02:00 UTC.
DAY1 = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
DAY = timedelta(days=1)

ALERT_LINE = "alert: code=LEDGER_INTEGRITY"


def at_wall(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(clock, "wall_now", lambda: moment)


def nightly_pass(app_engine: Engine) -> None:
    """One worker iteration without a sweep: reminders, deliveries, then the nightly step."""
    worker.run_iteration(app_engine, sweep=False)


def runs(engine: Engine) -> list[dict[str, Any]]:
    return rows(engine, "SELECT * FROM ledger_verification_run ORDER BY started_at, run_id")


def due(engine: Engine) -> bool:
    from app import ledger_ops

    value: bool = ledger_ops.nightly_due(engine)
    return value


def alerts(engine: Engine) -> list[dict[str, Any]]:
    """ops.current_alerts, imported here so each test fails by name until the code commit."""
    from app import ops

    with Session(engine) as session:
        return ops.current_alerts(session)


def ledger(engine: Engine) -> list[dict[str, Any]]:
    return rows(
        engine,
        "SELECT seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash"
        " FROM ledger_entry ORDER BY seq",
    )


def alert_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if ALERT_LINE in record.getMessage()]


def test_nightly_verify_records_a_valid_run(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_id = breach(client, fake_clock)
    view(client, event_id, "rev-1")
    entries = ledger(owner_engine)

    at_wall(monkeypatch, DAY1)
    assert due(app_engine) is True
    with caplog.at_level(logging.INFO):
        nightly_pass(app_engine)

    # One NIGHTLY row holding exactly verify's three fields, the head of the chain, and FIRST_RUN
    # because no earlier run exists. The ledger itself gained nothing (spec 6.1).
    [run] = runs(app_engine)
    assert run["run_id"].startswith("lvr-")
    assert run["trigger"] == "NIGHTLY"
    assert (run["started_at"], run["finished_at"]) == (DAY1, DAY1)
    assert (run["valid"], run["entries_checked"], run["first_mismatch"]) == (
        True,
        len(entries),
        None,
    )
    assert (run["head_seq"], run["head_hash"]) == (entries[-1]["seq"], entries[-1]["hash"])
    assert run["head_check"] == "FIRST_RUN"
    assert (run["projection_mismatches"], run["report_mismatches"]) == ([], [])
    assert ledger(owner_engine) == entries
    assert alerts(app_engine) == []
    assert alert_lines(caplog) == []
    assert due(app_engine) is False  # done for this UTC date

    # The next day, after more entries: a second run whose head is further on, and OK because
    # the first run's head is still in the chain with the same hash.
    view(client, event_id, "viewer-1")
    longer = ledger(owner_engine)
    at_wall(monkeypatch, DAY1 + DAY)
    nightly_pass(app_engine)
    first, second = runs(app_engine)
    assert first == run
    assert (second["valid"], second["entries_checked"]) == (True, len(longer))
    assert (second["head_seq"], second["head_check"]) == (longer[-1]["seq"], "OK")
    assert alerts(app_engine) == []

    # The table is insert-only, even for the owner (statement-level trigger, like 0006's).
    for statement in (
        "UPDATE ledger_verification_run SET valid = false",
        "DELETE FROM ledger_verification_run",
    ):
        with pytest.raises(DBAPIError), owner_engine.begin() as conn:
            conn.execute(text(statement))
    assert len(runs(app_engine)) == 2


def test_model5_ex5_nightly_verify_finds_a_payload_edit(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_id = breach(client, fake_clock)  # accuracy_drift 0.34 on sys-42
    view(client, event_id, "viewer-1")
    view(client, event_id, "rev-1")
    view(client, event_id, "viewer-1")
    entries = ledger(owner_engine)
    seqs = [entry["seq"] for entry in entries]
    [k] = [entry["seq"] for entry in entries if entry["entry_type"] == "EVENT_FLAGGED"]
    assert seqs[0] < k < seqs[-1]  # a middle entry
    assert entries[seqs.index(k)]["payload"]["observed_value"] == "0.34"

    # Model 5 Ex-5: as the owner, disable the trigger, change one character of the payload at
    # seq k ("0.34" -> "0.35"), re-enable the trigger.
    with trigger_disabled(owner_engine) as conn:
        conn.execute(
            text(
                "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}',"
                " '\"0.35\"') WHERE seq = :k"
            ),
            {"k": k},
        )

    at_wall(monkeypatch, DAY1)
    with caplog.at_level(logging.INFO):
        nightly_pass(app_engine)

    # Entry k's recomputed hash no longer matches its stored hash, so verification stops at k:
    # invalid, first_mismatch "k", k's position counted (app/ledger/verify.py).
    [run] = runs(app_engine)
    assert (run["valid"], run["first_mismatch"]) == (False, str(k))
    assert run["entries_checked"] == seqs.index(k) + 1
    # The edited payload now disagrees with the event row too (spec 6.3).
    assert run["projection_mismatches"] == [
        {
            "event_id": event_id,
            "column": "event.observed_value",
            "expected": "0.35",
            "actual": "0.34",
        }
    ]

    # Alert A1 is raised, critical, with ids and counts only (spec 7.2, 7.3).
    [alert] = alerts(app_engine)
    assert (alert["code"], alert["severity"]) == ("A1", "critical")
    assert alert["detail"]["run_id"] == run["run_id"]
    assert alert["detail"]["first_mismatch"] == str(k)

    # Exactly one ERROR line, with the seq and ids and no payload value.
    [line] = alert_lines(caplog)
    assert line.levelno == logging.ERROR
    message = line.getMessage()
    assert f"first_mismatch={k}" in message
    for value in ("0.34", "0.35", "0.30", "accuracy_drift", "sys-42"):
        assert value not in message


def test_nightly_verify_runs_once_per_utc_day(
    fake_clock: FakeClock, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import ledger_ops

    day1 = DAY1.replace(hour=0)

    # Before the hour (default 02:00 UTC): not due, and an iteration records nothing.
    at_wall(monkeypatch, day1.replace(hour=1, minute=59, second=59))
    assert due(app_engine) is False
    nightly_pass(app_engine)
    assert runs(app_engine) == []

    # From 02:00: due; two passes on the same UTC date give one NIGHTLY row.
    at_wall(monkeypatch, day1.replace(hour=2))
    assert due(app_engine) is True
    nightly_pass(app_engine)
    at_wall(monkeypatch, day1.replace(hour=2, minute=30))
    nightly_pass(app_engine)
    assert [run["started_at"] for run in runs(app_engine)] == [day1.replace(hour=2)]

    # The next date gives a second row.
    at_wall(monkeypatch, day1 + DAY + timedelta(hours=3))
    nightly_pass(app_engine)
    nightly_pass(app_engine)
    assert len(runs(app_engine)) == 2

    # A late start: the worker was down all day and comes back at 23:30; the run still happens.
    late = day1 + 2 * DAY + timedelta(hours=23, minutes=30)
    at_wall(monkeypatch, late)
    nightly_pass(app_engine)
    assert runs(app_engine)[-1]["started_at"] == late

    # Two workers at once on a fourth date: both find it due, both try, one row results.
    at_wall(monkeypatch, day1 + 3 * DAY + timedelta(hours=4))
    assert due(app_engine) is True
    barrier = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        try:
            barrier.wait()
            results.append(ledger_ops.run_verification(app_engine, "NIGHTLY"))
        except BaseException as exc:  # reported below, so a failure is never silent
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert len(results) == 2
    assert sum(result is not None for result in results) == 1
    assert len(runs(app_engine)) == 4
    assert all(run["trigger"] == "NIGHTLY" for run in runs(app_engine))

    # NIGHTLY_VERIFY_HOUR_UTC moves the hour: at 5, 04:59 is too early and 05:00 is due.
    monkeypatch.setenv("NIGHTLY_VERIFY_HOUR_UTC", "5")
    at_wall(monkeypatch, day1 + 4 * DAY + timedelta(hours=4, minutes=59))
    assert due(app_engine) is False
    at_wall(monkeypatch, day1 + 4 * DAY + timedelta(hours=5))
    assert due(app_engine) is True


def test_head_regression_is_detected(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_id = breach(client, fake_clock)
    view(client, event_id, "rev-1")
    view(client, event_id, "viewer-1")
    view(client, event_id, "viewer-1")
    entries = ledger(owner_engine)
    assert [entry["entry_type"] for entry in entries[-2:]] == ["EVENT_VIEWED", "EVENT_VIEWED"]

    at_wall(monkeypatch, DAY1)
    nightly_pass(app_engine)
    [first] = runs(app_engine)
    assert (first["head_seq"], first["head_check"]) == (entries[-1]["seq"], "FIRST_RUN")

    # The owner deletes the last two entries. What is left is a valid chain, and the two views
    # are not in the projection, so only the head comparison can see it: the head is now two
    # entries shorter than the previous run's head. REGRESSED, and A1.
    with trigger_disabled(owner_engine) as conn:
        conn.execute(
            text("DELETE FROM ledger_entry WHERE seq >= :seq"), {"seq": entries[-2]["seq"]}
        )
    at_wall(monkeypatch, DAY1 + DAY)
    with caplog.at_level(logging.INFO):
        nightly_pass(app_engine)
    _, second = runs(app_engine)
    assert (second["valid"], second["entries_checked"]) == (True, len(entries) - 2)
    assert (second["head_seq"], second["head_check"]) == (entries[-3]["seq"], "REGRESSED")
    assert (second["projection_mismatches"], second["report_mismatches"]) == ([], [])
    [alert] = alerts(app_engine)
    assert (alert["code"], alert["detail"]["run_id"]) == ("A1", second["run_id"])
    assert alert["detail"]["head_check"] == "REGRESSED"
    [line] = alert_lines(caplog)
    assert line.levelno == logging.ERROR
    assert "head_check=REGRESSED" in line.getMessage()

    # The other case: the head keeps its seq but its entry is rewritten with a recomputed hash.
    # The chain is valid again, yet the hash at the previous run's head seq has changed.
    head = ledger(owner_engine)[-1]
    head["payload"] = {**head["payload"], "note": "x"}
    new_hash = compute_hash(
        head["prev_hash"],
        canonical_json(
            seq=head["seq"],
            entry_type=head["entry_type"],
            actor_id=head["actor_id"],
            subject_id=head["subject_id"],
            payload=head["payload"],
            recorded_at=head["recorded_at"],
        ),
    )
    with trigger_disabled(owner_engine) as conn:
        conn.execute(
            text(
                "UPDATE ledger_entry SET payload = jsonb_set(payload, '{note}', '\"x\"'),"
                " hash = :hash WHERE seq = :seq"
            ),
            {"hash": new_hash, "seq": head["seq"]},
        )
    at_wall(monkeypatch, DAY1 + 2 * DAY)
    nightly_pass(app_engine)
    third = runs(app_engine)[-1]
    assert (third["valid"], third["head_seq"], third["head_hash"]) == (
        True,
        second["head_seq"],
        new_hash,
    )
    assert third["head_check"] == "REGRESSED"
    assert [alert["detail"]["run_id"] for alert in alerts(app_engine)] == [third["run_id"]]


@pytest.mark.integration
def test_tampered_report_bytes_are_found_by_the_nightly_check(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 6.1 step 4: the owner changes one byte of a stored report's json_bytes; the nightly
    run lists it. Needs Part 1's report assembly (red until Part 1 merges)."""
    event_id = breach(client, fake_clock)
    view(client, event_id, "rev-1")
    assert dispose(client, event_id, "OTHER_SERIOUS").status_code == 200
    url = f"/v1/events/{event_id}/report"
    report = get(client, url, actor="rev-1")
    assert report.status_code == 200, (
        f"{PART1_MISSING}: GET {url} gave {report.status_code} {report.text}"
    )
    report_id = report.headers["X-Report-Id"]

    # Untouched, the first run is clean.
    at_wall(monkeypatch, DAY1)
    nightly_pass(app_engine)
    [clean] = runs(app_engine)
    assert (clean["valid"], clean["report_mismatches"]) == (True, [])

    # One byte of the stored JSON flipped, as the owner, with report's own trigger disabled.
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE report DISABLE TRIGGER USER"))
        conn.execute(
            text(
                "UPDATE report SET json_bytes = set_byte(json_bytes, 10,"
                " get_byte(json_bytes, 10) # 1) WHERE report_id = :r"
            ),
            {"r": report_id},
        )
        conn.execute(text("ALTER TABLE report ENABLE TRIGGER USER"))

    at_wall(monkeypatch, DAY1 + DAY)
    with caplog.at_level(logging.INFO):
        nightly_pass(app_engine)
    run = runs(app_engine)[-1]
    # The ledger itself is untouched; only the stored bytes no longer match their hash.
    assert run["valid"] is True
    assert run["report_mismatches"] == [{"report_id": report_id, "which": "json_bytes"}]
    [alert] = alerts(app_engine)
    assert (alert["code"], alert["detail"]["run_id"]) == ("A1", run["run_id"])
    [line] = alert_lines(caplog)
    assert report_id in line.getMessage()
