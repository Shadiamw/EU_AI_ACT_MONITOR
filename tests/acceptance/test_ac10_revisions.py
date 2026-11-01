"""AC-10 (REQ-09): revising "Other serious incident" to "Death" keeps both values, the
reviewer's identity and the revision time; the original is not overwritten.

Contract Event: revisions is an array of {prior_tier, new_tier, reviewer_id, revised_at}.
Spec 2.7: disposition.tier is the tier in force; disposition.reviewer_id and assigned_at come
from the first assignment. Spec 2.10: DISPOSITION_RECORDED then DISPOSITION_REVISED (actor the
reviewer, subject the event), with prior_tier and new_tier in the revision's payload.
"""

from __future__ import annotations

import pytest

from tests.acceptance.api import Api, parse_ledger_time, parse_time

pytestmark = pytest.mark.integration

DAY = 86400


def test_a_revision_keeps_the_original_and_records_who_and_when(api: Api) -> None:
    event_id, _ = api.flag()
    first = api.disposed(event_id, "OTHER_SERIOUS")
    assigned_at = first["disposition"]["assigned_at"]
    assert first["revisions"] == []

    api.advance(3 * DAY)
    before = api.now()
    revised = api.disposed(event_id, "DEATH")
    after = api.now()

    assert revised["state"] == "DISPOSITIONED"
    assert revised["disposition"]["tier"] == "DEATH"
    assert revised["disposition"]["reviewer_id"] == "rev-1"
    assert revised["disposition"]["assigned_at"] == assigned_at  # the original assignment
    [revision] = revised["revisions"]
    assert set(revision) == {"prior_tier", "new_tier", "reviewer_id", "revised_at"}
    assert revision["prior_tier"] == "OTHER_SERIOUS"
    assert revision["new_tier"] == "DEATH"
    assert revision["reviewer_id"] == "rev-1"
    assert before <= parse_time(revision["revised_at"]) <= after

    # The same history on a later read.
    shown = api.event(event_id)
    assert shown["revisions"] == revised["revisions"]
    assert shown["disposition"] == revised["disposition"]


def test_the_ledger_holds_both_decisions(api: Api) -> None:
    event_id, _ = api.flag()
    api.disposed(event_id, "OTHER_SERIOUS")
    api.advance(DAY)
    api.disposed(event_id, "DEATH")

    decisions = [
        entry
        for entry in api.export()
        if entry["entry_type"] in ("DISPOSITION_RECORDED", "DISPOSITION_REVISED")
    ]
    assert [entry["entry_type"] for entry in decisions] == [
        "DISPOSITION_RECORDED",
        "DISPOSITION_REVISED",
    ]
    recorded, revised = decisions
    assert recorded["actor_id"] == revised["actor_id"] == "rev-1"
    assert recorded["subject_id"] == revised["subject_id"] == event_id
    assert recorded["payload"]["tier"] == "OTHER_SERIOUS"
    assert revised["payload"]["prior_tier"] == "OTHER_SERIOUS"
    assert revised["payload"]["new_tier"] == "DEATH"
    assert revised["payload"]["supersedes_id"] == recorded["payload"]["disposition_id"]
    assert parse_ledger_time(revised["recorded_at"]) > parse_ledger_time(recorded["recorded_at"])

    trail = [(row["actor_id"], row["action"]) for row in api.audit(event_id)]
    assert ("rev-1", "DISPOSITION_RECORDED") in trail
    assert ("rev-1", "DISPOSITION_REVISED") in trail
    assert api.verify()["valid"] is True


def test_a_same_tier_revision_is_listed_too(api: Api) -> None:
    """Spec 2.7: a same-tier revision is listed with prior_tier equal to new_tier."""
    event_id, _ = api.flag()
    first = api.disposed(event_id, "DEATH")
    again = api.disposed(event_id, "DEATH")
    assert [(r["prior_tier"], r["new_tier"]) for r in again["revisions"]] == [("DEATH", "DEATH")]
    assert again["deadline_utc"] == first["deadline_utc"]
