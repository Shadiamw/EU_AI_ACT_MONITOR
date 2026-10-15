"""Part 3 end to end, and the worker loop.

The scenario follows integration_notes.md section 8, Part 3: register a webhook, let a system
go silent past the timeout, run the worker, deliver to a local receiver, send a sample, then
re-verify the exported ledger with the standalone script.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import gaps, worker
from tests.api.conftest import ApiClient, as_actor, build_app, seed
from tests.script_helpers import run_subprocess
from tests.worker.conftest import T0, FakeClock, Receiver, add_system, entry_types, rows


def test_gap_to_delivery_to_close_and_reverify(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    tmp_path: Path,
) -> None:
    seed(app_engine)
    add_system(owner_engine, "sys-42", last_received_at=T0)
    with ApiClient(build_app()) as client:
        response = client.request(
            "POST",
            "/v1/webhooks",
            json={"url": receiver.url, "event_types": ["flag_notification"]},
            headers=as_actor("rev-1"),
        )
        assert response.status_code == 201
        webhook_id = response.json()["webhook_id"]

        # AC-02 boundary: 2 intervals withheld (10 minutes), the worker does nothing.
        fake_clock.advance(600)
        worker.run_iteration(app_engine, sweep=True)
        assert entry_types(owner_engine) == ["SEED", "WEBHOOK_REGISTERED"]

        # AC-02: exactly 3 intervals (15 minutes) withheld. One iteration opens the gap and
        # delivers its notification.
        fake_clock.advance(300)
        worker.run_iteration(app_engine, sweep=True)
        assert entry_types(owner_engine) == [
            "SEED",
            "WEBHOOK_REGISTERED",
            "GAP_OPENED",
            "NOTIFICATION_REQUESTED",
            "NOTIFICATION_DELIVERED",
        ]
        [event] = rows(owner_engine, "SELECT event_id, type, state FROM event")
        event_id = event["event_id"]
        assert (event["type"], event["state"]) == ("monitoring_gap", "FLAGGED")
        # Spec 3.5 body: the gap was detected at T0 + 900 s and sent in the same iteration.
        assert receiver.calls[-1].body == {
            "notification_type": "flag_notification",
            "notification_id": receiver.calls[-1].body["notification_id"],
            "event_id": event_id,
            "system_id": "sys-42",
            "type": "monitoring_gap",
            "detected_at": "2026-10-01T09:15:00Z",
            "sent_at": "2026-10-01T09:15:00Z",
        }
        assert receiver.calls[-1].body["notification_id"].startswith("ntf-")
        [delivery] = rows(owner_engine, "SELECT webhook_id FROM notification_delivery")
        assert delivery["webhook_id"] == webhook_id
        [delivered] = rows(
            owner_engine,
            "SELECT payload FROM ledger_entry WHERE entry_type = 'NOTIFICATION_DELIVERED'",
        )
        assert delivered["payload"]["latency_seconds"] == 0
        assert delivered["payload"]["within_bound"] is True
        # AC-02's expected result: the reviewer is notified within 15 minutes of the third
        # missed interval (T0 + 900 s), read from the ledger as a grader would.
        [delivered_at] = rows(
            owner_engine,
            "SELECT recorded_at FROM ledger_entry WHERE entry_type = 'NOTIFICATION_DELIVERED'",
        )
        third_missed = T0 + timedelta(seconds=900)
        delivered_moment = datetime.fromisoformat(delivered_at["recorded_at"])
        assert timedelta(0) <= delivered_moment - third_missed <= timedelta(minutes=15)

        audit = client.get(f"/v1/events/{event_id}/audit", headers=as_actor("viewer-1"))
        assert [(e["actor_id"], e["action"]) for e in audit.json()] == [
            ("system", "GAP_OPENED"),
            ("system", "NOTIFICATION_REQUESTED"),
            ("system", "NOTIFICATION_DELIVERED"),
        ]

        # Data resumes 1000 s after the last sample, 100 s after the gap was detected: Part 1's
        # ingest closes the gap.
        fake_clock.advance(100)
        with Session(app_engine) as session:
            assert gaps.close_if_open(session, "sys-42") == event_id
            session.execute(
                text("UPDATE monitored_system SET last_received_at = :now"),
                {"now": fake_clock.now()},
            )
            session.commit()
        [closed] = rows(
            owner_engine, "SELECT payload FROM ledger_entry WHERE entry_type = 'GAP_CLOSED'"
        )
        assert closed["payload"]["gap_seconds"] == 100  # from detection (spec 3.4)

        export = client.get("/v1/ledger/export", headers=as_actor("rev-1"))
        verify = client.get("/v1/ledger/verify", headers=as_actor("rev-1"))
    # SEED, WEBHOOK_REGISTERED, GAP_OPENED, NOTIFICATION_REQUESTED, NOTIFICATION_DELIVERED,
    # AUDIT_VIEWED (the audit call above) and GAP_CLOSED.
    assert verify.json() == {"valid": True, "entries_checked": 7, "first_mismatch": None}
    export_file = tmp_path / "export.json"
    export_file.write_bytes(export.content)
    run = run_subprocess([str(export_file)])
    assert (run.code, run.result) == (0, verify.json())
    assert [e["entry_type"] for e in json.loads(export.content)][-1] == "GAP_CLOSED"


class StopLoop(BaseException):
    pass


def test_the_loop_sweeps_every_minute_and_survives_a_failed_iteration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticks = iter([0.0, 0.0, 5.0, 59.9, 60.0, 65.0])  # start, then one per iteration
    sweeps: list[bool] = []
    sleeps: list[float] = []

    def fake_iteration(engine: Any, *, sweep: bool) -> None:
        sweeps.append(sweep)
        if len(sweeps) == 1:
            raise RuntimeError("boom")

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise StopLoop

    monkeypatch.setattr(worker, "run_iteration", fake_iteration)
    monkeypatch.setattr(worker, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker, "sleep", fake_sleep)
    with caplog.at_level(logging.ERROR, logger="worker"), pytest.raises(StopLoop):
        worker.run_forever(None)  # type: ignore[arg-type]
    assert sweeps == [True, False, False, True, False]
    assert sleeps == [5, 5, 5, 5, 5]
    assert "worker iteration failed: type=RuntimeError" in caplog.text
    assert "boom" not in caplog.text
