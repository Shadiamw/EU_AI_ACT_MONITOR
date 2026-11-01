"""AC-17 (REQ-15): after a simulated 4 h wait before assignment, the detection timestamp and the
assignment-anchored deadline are recorded and independently visible, with the gap between them
as the risk indicator.

Spec Part 1 1.6: the Event already carries detected_at, disposition.assigned_at and
deadline_utc; the gap is assigned_at - detected_at (now - detected_at before a disposition); no
API field is added. Demo step 7: assigned_at - detected_at is about 4 h.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.acceptance.api import Api, api_time, parse_time

pytestmark = pytest.mark.integration

FOUR_HOURS = timedelta(hours=4)


def test_detection_and_the_assignment_deadline_are_both_visible_with_the_gap(api: Api) -> None:
    event_id, detected = api.flag()
    before = api.event(event_id)
    assert before["detected_at"] == api_time(detected)
    assert before["disposition"] is None and before["deadline_utc"] is None
    # Before assignment the indicator is now - detected_at.
    assert timedelta(0) <= api.now() - detected < timedelta(minutes=1)

    api.advance(int(FOUR_HOURS.total_seconds()))
    assert FOUR_HOURS <= api.now() - detected < FOUR_HOURS + timedelta(minutes=1)
    api.disposed(event_id, "OTHER_SERIOUS")

    event = api.event(event_id)
    assert event["detected_at"] == api_time(detected)  # written once, never moved
    assigned = parse_time(event["disposition"]["assigned_at"])
    deadline = parse_time(event["deadline_utc"])
    gap = assigned - parse_time(event["detected_at"])
    assert FOUR_HOURS <= gap < FOUR_HOURS + timedelta(minutes=1)
    assert deadline - assigned == timedelta(days=15)
    assert deadline - parse_time(event["detected_at"]) == timedelta(days=15) + gap
