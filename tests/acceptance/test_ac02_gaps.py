"""AC-02 (REQ-02): withholding 3 intervals (15 min) opens a gap event and notifies the reviewer;
withholding 2 must not.

Phase 3 spec 3.4: silence = now - last_received_at, measured on the server; a gap opens when
silence >= 3 x 300 s and the system has no open gap. Reset seeds last_received_at, and a clock
advance runs the sweeper (spec 4.5), so the boundary is exact in simulated time.
Gap event fields: Phase 3 spec 3.4. Flag body: Phase 3 spec 3.5.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.acceptance.api import (
    HARM_KEYS,
    PERF_METRIC,
    Api,
    Sink,
    bodies,
    delivered_payload,
    parse_time,
    wait_until,
)

pytestmark = pytest.mark.integration

INTERVAL = 300


def test_two_withheld_intervals_open_no_gap(api: Api) -> None:
    api.advance(2 * INTERVAL)
    assert api.events(type="monitoring_gap") == []


def test_three_withheld_intervals_open_one_gap_event(api: Api) -> None:
    api.advance(2 * INTERVAL)
    assert api.events(type="monitoring_gap") == []
    detected = api.advance(INTERVAL)

    gaps = api.events(type="monitoring_gap")
    assert len(gaps) == 1, gaps
    event = api.event(gaps[0]["event_id"])
    assert event["type"] == "monitoring_gap"
    assert event["system_id"] == "sys-42"
    assert event["state"] == "FLAGGED"
    assert (event["metric_name"], event["observed_value"], event["threshold_value"]) == (
        None,
        None,
        None,
    )
    assert abs((parse_time(event["detected_at"]) - detected).total_seconds()) < 60
    assert event["disposition"] is None and event["deadline_utc"] is None
    assert event["revisions"] == []
    for key in HARM_KEYS:
        assert event["harm_fields"][key] == {
            "status": "NOT_APPLICABLE",
            "evidence": None,
            "confirmed_by": None,
            "confirmed_at": None,
        }

    # One event per outage, however long it lasts.
    api.advance(10 * INTERVAL)
    assert len(api.events(type="monitoring_gap")) == 1


def test_a_sample_after_the_outage_closes_the_gap(api: Api) -> None:
    api.advance(3 * INTERVAL)
    [gap] = api.events(type="monitoring_gap")
    response = api.ingest(PERF_METRIC, "0.90")
    assert response.status_code == 201, response.text
    assert api.event(gap["event_id"])["state"] == "CLOSED"


def test_the_gap_is_notified_within_the_configured_delay(api: Api, sink: Sink) -> None:
    webhook_id = api.register_webhook(sink.url, ["flag_notification"])
    api.advance(3 * INTERVAL)
    [gap] = api.events(type="monitoring_gap")

    received = wait_until(
        lambda: bodies(sink.received(), "flag_notification"), lambda found: len(found) >= 1
    )
    assert len(received) == 1, received
    body = received[0]
    assert set(body) == {
        "notification_type",
        "notification_id",
        "event_id",
        "system_id",
        "type",
        "detected_at",
        "sent_at",
    }, body
    assert body["event_id"] == gap["event_id"]
    assert body["type"] == "monitoring_gap"
    assert body["system_id"] == "sys-42"
    assert body["notification_id"].startswith("ntf-")
    assert body["detected_at"] == gap["detected_at"]

    delivered = delivered_payload(api, body["notification_id"])
    assert delivered["webhook_id"] == webhook_id
    assert delivered["within_bound"] in (True, "true"), delivered
    assert Decimal(str(delivered["latency_seconds"])) <= 900
