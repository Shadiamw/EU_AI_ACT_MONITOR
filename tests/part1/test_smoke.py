"""The integration smoke test of spec section 9, over the real HTTP API.

Part 4's reset and clock-advance endpoints are used for steps 1 and 4. The verifier runs as
a separate process, as another team would run it. The fake clock is not used: advance moves
clock_state.offset_seconds, and clock.now() reads it (decision 133).
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.main import create_app
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import EXAMPLE_1, get
from tests.script_helpers import run_subprocess
from tests.worker.conftest import Receiver


@pytest.fixture
def live_client() -> Iterator[ApiClient]:
    with ApiClient(create_app()) as api_client:
        yield api_client


def test_spec_section_9_smoke(
    live_client: ApiClient,
    receiver: Receiver,
    tmp_path: Path,
) -> None:
    client = live_client
    # 1. Reset. The ledger is valid with one entry, SEED.
    assert client.request("POST", "/v1/test/reset").json() == {"ok": True}
    assert get(client, "/v1/ledger/verify").json() == {
        "valid": True,
        "entries_checked": 1,
        "first_mismatch": None,
    }
    assert [e["entry_type"] for e in get(client, "/v1/ledger/export").json()] == ["SEED"]

    # 2. Register a webhook; accuracy_drift = 0.34 returns 201 with a flagged_event_id.
    #    Advance 1 s so the delivery pass in clock/advance sends the notification (spec 4.6).
    registered = client.request(
        "POST",
        "/v1/webhooks",
        headers=bearer("rev-1"),
        json={"url": receiver.url, "event_types": ["flag_notification"]},
    )
    assert registered.status_code == 201
    pings = len(receiver.calls)
    # Use a timestamp at "now" so the sample is accepted under the live clock.
    from app import clock

    sample = {
        **EXAMPLE_1,
        "timestamp": clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    response = client.request("POST", "/v1/metrics", headers=bearer("sys-42"), json=sample)
    assert response.status_code == 201
    event_id = response.json()["flagged_event_id"]
    delivered = client.request("POST", "/v1/test/clock/advance", json={"seconds": 1})
    assert delivered.status_code == 200
    assert delivered.json()["deliveries"] == 1
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

    # 4. Advance 900 s more with no samples: a monitoring_gap event appears and a second
    #    webhook call arrives.
    advanced = client.request("POST", "/v1/test/clock/advance", json={"seconds": 900})
    assert advanced.status_code == 200
    assert len(advanced.json()["gaps_opened"]) == 1
    gap_id = advanced.json()["gaps_opened"][0]
    assert advanced.json()["deliveries"] >= 1
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

    [flagged] = [e for e in export if e["entry_type"] == "EVENT_FLAGGED"]
    assert flagged["seq"] == 3
    flagged["payload"]["observed_value"] = "0.35"
    dirty = tmp_path / "tampered.json"
    dirty.write_text(json.dumps(export), encoding="utf-8")
    bad = run_subprocess([str(dirty)])
    assert bad.code == 1
    assert bad.result == {
        "valid": False,
        "entries_checked": 3,
        "first_mismatch": "3",
    }
