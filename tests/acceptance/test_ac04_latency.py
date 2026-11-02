"""AC-04 (REQ-04): the reviewer notification for a flagged event is delivered within 15 min.

Phase 3 spec 3.5: the flag_notification body, and NOTIFICATION_DELIVERED with latency_seconds
(delivery time minus the event's received_at) and within_bound (latency <= 900 s by default).
The stack's worker delivers it; the clock is not moved, so the latency is real time.

The boundary ("re-test at an alternate configured value, e.g. 5 min") needs a restart with
another NOTIFY_MAX_DELAY_SECONDS, which HTTP cannot do; the in-process tests cover it
(README 13).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.acceptance.api import Api, Sink, bodies, delivered_payload, wait_until

pytestmark = pytest.mark.integration

BOUND_SECONDS = 900


def test_the_flag_notification_is_delivered_within_the_bound(api: Api, sink: Sink) -> None:
    webhook_id = api.register_webhook(sink.url, ["flag_notification"])
    event_id, _ = api.flag()
    event = api.event(event_id)

    received = wait_until(
        lambda: bodies(sink.received(), "flag_notification"),
        lambda found: len(found) >= 1,
        timeout=60.0,
    )
    assert len(received) == 1, received
    body = received[0]
    assert body == {
        "notification_type": "flag_notification",
        "notification_id": body["notification_id"],
        "event_id": event_id,
        "system_id": "sys-42",
        "type": "threshold_breach",
        "detected_at": event["detected_at"],
        "sent_at": body["sent_at"],
    }
    assert body["notification_id"].startswith("ntf-")

    delivered = delivered_payload(api, body["notification_id"])
    assert delivered["webhook_id"] == webhook_id
    assert delivered["within_bound"] in (True, "true"), delivered
    assert Decimal(str(delivered["latency_seconds"])) <= BOUND_SECONDS

    trail = [row["action"] for row in api.audit(event_id) if not row["action"].endswith("VIEWED")]
    assert trail == ["EVENT_FLAGGED", "NOTIFICATION_REQUESTED", "NOTIFICATION_DELIVERED"]


def test_a_webhook_registered_only_for_reminders_gets_no_flag(api: Api, sink: Sink) -> None:
    """Phase 3 spec 3.5 routing: only webhooks whose event_types include the type."""
    api.register_webhook(sink.url, ["reminder"])
    api.flag()
    api.now()  # a delivery pass (spec 4.5)
    assert bodies(sink.received(), "flag_notification") == []
