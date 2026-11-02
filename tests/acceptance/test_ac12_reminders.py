"""AC-12 (REQ-11): with an active 15-day window, a reminder fires at 50%, 80% and 100% of the
elapsed window and not at 49%, 79% or 99%.

Spec section 6: the instant for p is anchor + ceil((deadline - anchor) * p / 100) in whole
microseconds; for 15 days the 49/50/79/80/99/100 offsets are +7d 08:24, +7d 12:00, +11d 20:24,
+12d, +14d 20:24, +15d. Spec 3.4: a reminder is sent at or after its instant, never before, at
most once per event and percentage; a clock advance runs the firing and delivery passes
(spec 4.5). Spec 3.5 pins the reminder body. Addendum rule 7: the webhook is registered for
reminder only, so no gap flag reaches it, and counts filter by notification_type anyway.

Needs Part 3 (escalations and reminders), merged at integration (decision 215).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.acceptance.api import Api, Sink, bodies, instant, parse_time, wait_until

pytestmark = pytest.mark.integration

STEPS = [(49, []), (50, [50]), (79, [50]), (80, [50, 80]), (99, [50, 80]), (100, [50, 80, 100])]
OFFSETS = {
    49: timedelta(days=7, hours=8, minutes=24),
    50: timedelta(days=7, hours=12),
    79: timedelta(days=11, hours=20, minutes=24),
    80: timedelta(days=12),
    99: timedelta(days=14, hours=20, minutes=24),
    100: timedelta(days=15),
}
REMINDER_KEYS = {
    "notification_type",
    "notification_id",
    "event_id",
    "system_id",
    "type",
    "detected_at",
    "tier",
    "assigned_at",
    "deadline_utc",
    "escalation_pct",
    "due_at",
    "sent_at",
}


def _reminders(sink: Sink) -> list[dict[str, Any]]:
    return bodies(sink.received(), "reminder")


def _at_least(count: int) -> Callable[[list[dict[str, Any]]], bool]:
    return lambda found: len(found) >= count


def _pcts(found: list[dict[str, Any]]) -> list[int]:
    return [int(body["escalation_pct"]) for body in found]


def _dispositioned(api: Api, sink: Sink) -> tuple[dict[str, Any], datetime, datetime]:
    api.register_webhook(sink.url, ["reminder"])
    event_id, _ = api.flag()
    event = api.disposed(event_id, "OTHER_SERIOUS")
    anchor = parse_time(event["disposition"]["assigned_at"])
    deadline = parse_time(event["deadline_utc"])
    assert deadline - anchor == timedelta(days=15)
    return event, anchor, deadline


def test_the_instants_are_the_spec_table() -> None:
    """The formula against the pinned table, independent of the stack."""
    anchor = datetime.fromisoformat("2026-09-27T10:12:00+00:00")
    deadline = anchor + timedelta(days=15)
    for pct, offset in OFFSETS.items():
        assert instant(anchor, deadline, pct) - anchor == offset, pct


def test_reminders_fire_at_50_80_and_100_percent_and_never_early(api: Api, sink: Sink) -> None:
    event, anchor, deadline = _dispositioned(api, sink)
    assert _reminders(sink) == []

    for pct, expected in STEPS:
        reached = api.advance_to(instant(anchor, deadline, pct))
        assert reached - anchor < OFFSETS[pct] + timedelta(minutes=1)
        if expected and expected[-1] == pct:
            found = wait_until(lambda: _reminders(sink), _at_least(len(expected)))
        else:
            found = _reminders(sink)
        assert _pcts(found) == expected, f"at {pct}% the sink holds reminders {_pcts(found)}"

    api.advance(86400)
    found = _reminders(sink)
    assert _pcts(found) == [50, 80, 100]
    assert len({body["notification_id"] for body in found}) == 3


def test_the_reminder_body_is_the_pinned_shape(api: Api, sink: Sink) -> None:
    event, anchor, deadline = _dispositioned(api, sink)
    api.advance_to(instant(anchor, deadline, 50))
    found = wait_until(lambda: _reminders(sink), _at_least(1))
    assert len(found) == 1, f"no reminder at 50%: the sink holds {found}"
    body = found[0]
    assert set(body) == REMINDER_KEYS
    assert body["notification_type"] == "reminder"
    assert body["notification_id"].startswith("ntf-")
    assert body["event_id"] == event["event_id"]
    assert body["system_id"] == "sys-42"
    assert body["type"] == "threshold_breach"
    assert body["detected_at"] == event["detected_at"]
    assert body["tier"] == "OTHER_SERIOUS"
    assert body["assigned_at"] == event["disposition"]["assigned_at"]
    assert body["deadline_utc"] == event["deadline_utc"]
    assert body["escalation_pct"] == 50
    due = parse_time(body["due_at"])
    assert due == instant(anchor, deadline, 50)
    assert parse_time(body["sent_at"]) >= due

    trail = [(row["actor_id"], row["action"]) for row in api.audit(event["event_id"])]
    assert ("system", "REMINDER_TRIGGERED") in trail
