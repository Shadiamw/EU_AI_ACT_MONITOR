#!/usr/bin/env python3
"""Phase 4 pod demo over HTTP only (spec 4.1 / 4.13).

Needs BASE_URL, the three seeded keys, and a reachable webhook sink. Against stubs on a
part branch this script is expected to stop at disposition; after integration it walks
flag → review → disposition → clock reminders.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
INGEST = os.environ.get("INGEST_KEY") or os.environ.get(
    "INGEST_KEY_SYS_42", "test-ingest-key-sys-42-0d6f1c2e9b8a4f37"
)
REVIEWER = os.environ.get("REVIEWER_KEY", "test-reviewer-key-rev-1-5a7e3b9c1d2f4086")
VIEWER = os.environ.get("VIEWER_KEY", "test-viewer-key-viewer-1-8c4d2e6f0a1b3957")
SINK = os.environ.get("WEBHOOK_SINK_URL", "http://127.0.0.1:8765/hook/demo")


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def show(label: str, response: httpx.Response) -> Any:
    print(f"== {label} ==")
    print(f"HTTP {response.status_code}")
    if response.content:
        print(response.text)
    if response.status_code >= 400:
        raise SystemExit(f"demo failed at {label}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        show("health", client.get("/healthz"))
        show("reset", client.post("/v1/test/reset", headers=auth(REVIEWER)))
        now = show(
            "simulated now",
            client.post(
                "/v1/test/clock/advance",
                headers=auth(REVIEWER),
                json={"seconds": 0},
            ),
        )["simulated_time"]
        show(
            "register webhook",
            client.post(
                "/v1/webhooks",
                headers=auth(REVIEWER),
                json={"url": SINK, "event_types": ["flag_notification", "reminder"]},
            ),
        )
        flagged = show(
            "ingest accuracy_drift 0.34",
            client.post(
                "/v1/metrics",
                headers=auth(INGEST),
                json={
                    "system_id": "sys-42",
                    "metric_name": "accuracy_drift",
                    "metric_type": "drift",
                    "value": 0.34,
                    "timestamp": now,
                },
            ),
        )
        event_id = flagged["flagged_event_id"]
        show(
            "deliver flag",
            client.post(
                "/v1/test/clock/advance",
                headers=auth(REVIEWER),
                json={"seconds": 1},
            ),
        )
        detail = show(
            "reviewer opens event",
            client.get(f"/v1/events/{event_id}", headers=auth(REVIEWER)),
        )
        print(f"state={detail.get('state')} deadline={detail.get('deadline_utc')}")
        disposition = client.post(
            f"/v1/events/{event_id}/disposition",
            headers=auth(REVIEWER),
            json={
                "tier": "OTHER_SERIOUS",
                "rationale": "demo: rights impact under review",
                "harm_fields": {
                    "health_safety_harm": {"status": "NOT_APPLICABLE"},
                    "critical_infrastructure_disruption": {"status": "NOT_APPLICABLE"},
                    "fundamental_rights_infringement": {"status": "CONFIRMED"},
                    "property_environmental_damage": {"status": "NOT_APPLICABLE"},
                },
            },
        )
        print(f"== disposition ==\nHTTP {disposition.status_code}\n{disposition.text}")
        if disposition.status_code == 501:
            print("disposition still a stub on this branch; demo stops before reminders")
            return 0
        if disposition.status_code >= 400:
            return 1
        show(
            "viewer list gaps after 900s",
            client.post(
                "/v1/test/clock/advance",
                headers=auth(REVIEWER),
                json={"seconds": 900},
            ),
        )
        show("gaps", client.get("/v1/events?type=monitoring_gap", headers=auth(VIEWER)))
        show("verify", client.get("/v1/ledger/verify", headers=auth(VIEWER)))
    print("demo ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
