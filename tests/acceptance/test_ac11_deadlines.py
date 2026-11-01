"""AC-11 (REQ-10): the deadline is computed from the assignment timestamp, not the detection
timestamp, in calendar days; a stricter revision shortens it from the original assignment; a
looser one never extends it without a logged rationale.

Spec section 6 and 2.5 (default calendar_days): CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD 2 days,
DEATH 10, OTHER_SERIOUS 15; deadline_utc = anchor + days, anchor = the first assigned_at.
The 4 h advance before assigning makes "from assignment, not detection" observable.
README 10.7: these values hold only for DEADLINE_CALENDAR=calendar_days (the default).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.acceptance.api import DEADLINE_EXTENSION, TIER_DAYS, Api, parse_time

pytestmark = pytest.mark.integration

FOUR_HOURS = 4 * 3600
DAY = 86400


@pytest.mark.parametrize(
    ("tier", "hours"),
    [
        ("CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", 48),
        ("DEATH", 240),
        ("OTHER_SERIOUS", 360),
    ],
)
def test_the_deadline_is_exactly_the_window_after_assignment(
    api: Api, tier: str, hours: int
) -> None:
    event_id, detected = api.flag()
    api.advance(FOUR_HOURS)
    event = api.disposed(event_id, tier)

    assigned = parse_time(event["disposition"]["assigned_at"])
    deadline = parse_time(event["deadline_utc"])
    assert deadline - assigned == timedelta(hours=hours) == timedelta(days=TIER_DAYS[tier])
    assert deadline - detected > timedelta(hours=hours)  # not anchored at detection
    assert parse_time(event["detected_at"]) == detected


def test_a_stricter_revision_shortens_the_deadline_from_the_original_assignment(
    api: Api,
) -> None:
    event_id, _ = api.flag()
    first = api.disposed(event_id, "OTHER_SERIOUS")
    anchor = parse_time(first["disposition"]["assigned_at"])
    assert parse_time(first["deadline_utc"]) == anchor + timedelta(days=15)

    api.advance(3 * DAY)
    revised = api.disposed(event_id, "DEATH")
    assert parse_time(revised["deadline_utc"]) == anchor + timedelta(days=10)
    assert parse_time(revised["disposition"]["assigned_at"]) == anchor
    revised_at = parse_time(revised["revisions"][0]["revised_at"])
    assert parse_time(revised["deadline_utc"]) < revised_at + timedelta(days=10)


def test_a_looser_revision_without_a_rationale_does_not_extend_the_deadline(api: Api) -> None:
    event_id, _ = api.flag()
    first = api.disposed(event_id, "DEATH")
    deadline = first["deadline_utc"]
    api.advance(DAY)
    before = api.entries()

    for rationale in (None, "", "   "):
        response = api.dispose(event_id, "OTHER_SERIOUS", rationale=rationale)
        assert response.status_code == 409, response.text
        assert response.json() == DEADLINE_EXTENSION

    assert api.entries() == before
    event = api.event(event_id)
    assert event["deadline_utc"] == deadline
    assert event["disposition"]["tier"] == "DEATH"
    assert event["revisions"] == []


def test_a_looser_revision_with_a_rationale_extends_it_and_logs_the_rationale(
    api: Api,
) -> None:
    event_id, _ = api.flag()
    first = api.disposed(event_id, "DEATH")
    anchor = parse_time(first["disposition"]["assigned_at"])
    api.advance(DAY)

    reason = "no death occurred; the affected record was a test account"
    revised = api.disposed(event_id, "OTHER_SERIOUS", rationale=reason)
    assert parse_time(revised["deadline_utc"]) == anchor + timedelta(days=15)
    assert revised["disposition"]["tier"] == "OTHER_SERIOUS"
    assert revised["disposition"]["rationale"] == reason
    assert [(r["prior_tier"], r["new_tier"]) for r in revised["revisions"]] == [
        ("DEATH", "OTHER_SERIOUS")
    ]

    [entry] = [e for e in api.export() if e["entry_type"] == "DISPOSITION_REVISED"]
    assert entry["payload"]["rationale"] == reason
    assert entry["payload"]["prior_deadline_utc"] == first["deadline_utc"]
    assert entry["payload"]["deadline_utc"] == revised["deadline_utc"]
