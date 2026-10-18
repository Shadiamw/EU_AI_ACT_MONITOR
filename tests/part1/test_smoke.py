"""The integration smoke test of spec section 9, in process, for Parts 1 to 3 together.

Part 4 owns the real smoke test: one script, run in CI and against the deployed service. Two
of its steps call Part 4 endpoints that do not exist yet. Until they land, this test does
their work in process; each such step is marked PENDING with the endpoint it waits for:

- Step 1, POST /v1/test/reset (PENDING): the `seeded` fixture truncates the tables and runs
  the seed stand-in (tools/seed_part1.py), which appends SEED.
- Step 4, POST /v1/test/clock/advance (PENDING): the fake clock moves 900 s, then
  gaps.sweep and notifications.deliver_due run once each, as spec 4.6 says the endpoint does.

Every other step goes through the real HTTP API with real Bearer keys, and the verifier runs
as a separate process, as the other team would run it.
"""

import json
from pathlib import Path

from sqlalchemy import Engine

from app import gaps, notifications
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import EXAMPLE_1, get
from tests.script_helpers import run_subprocess
from tests.worker.conftest import FakeClock, Receiver


def test_spec_section_9_smoke_in_process(
    client: ApiClient,
    app_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    tmp_path: Path,
) -> None:
    # 1. Reset (PENDING: POST /v1/test/reset; done by the fixture). The ledger is valid with
    #    one entry, SEED.
    assert get(client, "/v1/ledger/verify").json() == {
        "valid": True,
        "entries_checked": 1,
        "first_mismatch": None,
    }
    assert [e["entry_type"] for e in get(client, "/v1/ledger/export").json()] == ["SEED"]

    # 2. Register a webhook; accuracy_drift = 0.34 returns 201 with a flagged_event_id; the
    #    webhook receives one flag_notification for that event.
    registered = client.request(
        "POST",
        "/v1/webhooks",
        headers=bearer("rev-1"),
        json={"url": receiver.url, "event_types": ["flag_notification"]},
    )
    assert registered.status_code == 201
    pings = len(receiver.calls)
    response = client.request("POST", "/v1/metrics", headers=bearer("sys-42"), json=EXAMPLE_1)
    assert response.status_code == 201
    event_id = response.json()["flagged_event_id"]
    assert notifications.deliver_due(app_engine) == 1
    deliveries = receiver.calls[pings:]
    assert [(c.body["notification_type"], c.body["event_id"]) for c in deliveries] == [
        ("flag_notification", event_id)
    ]

    # 3. The detail is a FLAGGED breach; the audit trail lists the four entries.
    detail = get(client, f"/v1/events/{event_id}").json()
    assert (detail["type"], detail["state"]) == ("threshold_breach", "FLAGGED")
    trail = get(client, f"/v1/events/{event_id}/audit").json()
    assert [item["action"] for item in trail] == [
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
        "NOTIFICATION_DELIVERED",
        "EVENT_VIEWED",
    ]

    # 4. Advance 900 s with no samples (PENDING: POST /v1/test/clock/advance): a
    #    monitoring_gap event appears and a second webhook call arrives.
    fake_clock.advance(900)
    [gap_id] = gaps.sweep(app_engine)
    assert notifications.deliver_due(app_engine) == 1
    gaps_listed = get(client, "/v1/events?type=monitoring_gap").json()
    assert [item["event_id"] for item in gaps_listed] == [gap_id]
    deliveries = receiver.calls[pings:]
    assert [(c.body["event_id"], c.body["type"]) for c in deliveries] == [
        (event_id, "threshold_breach"),
        (gap_id, "monitoring_gap"),
    ]

    # 5. Export, change one payload offline, run the standalone verifier: it reports the first
    #    bad sequence number.
    export = get(client, "/v1/ledger/export").json()
    clean = tmp_path / "export.json"
    clean.write_text(json.dumps(export), encoding="utf-8")
    run = run_subprocess([str(clean)])
    assert run.code == 0
    assert run.result == {"valid": True, "entries_checked": len(export), "first_mismatch": None}

    # SEED, WEBHOOK_REGISTERED, then EVENT_FLAGGED (spec 2.4).
    [flagged] = [e for e in export if e["entry_type"] == "EVENT_FLAGGED"]
    assert flagged["seq"] == 3
    flagged["payload"]["observed_value"] = "0.35"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(export), encoding="utf-8")
    run = run_subprocess([str(tampered)])
    assert run.code != 0
    assert run.result == {"valid": False, "entries_checked": 3, "first_mismatch": "3"}
