"""AC-08 (REQ-08a): an event with no reviewer-assigned disposition cannot advance to reporting
status; the attempt is rejected and the event stays in its prior status.

Spec Part 2 2.8 and D1: GET /v1/events/{id}/report is REVIEWER only; 404 for an unknown event;
409 NO_DISPOSITION with no disposition (gap events always), state unchanged and nothing
written; 501 NOT_IMPLEMENTED once a disposition exists (the report itself is Phase 5).
"""

from __future__ import annotations

import pytest

from tests.acceptance.api import NO_DISPOSITION, NOT_FOUND, NOT_IMPLEMENTED, Api

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("opened", [False, True], ids=["flagged", "under-review"])
def test_the_report_without_a_disposition_is_409_and_changes_nothing(
    api: Api, opened: bool
) -> None:
    event_id, _ = api.flag()
    if opened:
        api.event(event_id, role="reviewer")  # D3: FLAGGED -> UNDER_REVIEW
    prior = api.event(event_id)["state"]
    assert prior == ("UNDER_REVIEW" if opened else "FLAGGED")
    before = api.entries()

    response = api.report(event_id)
    assert response.status_code == 409, response.text
    assert response.json() == NO_DISPOSITION

    assert api.entries() == before
    after = api.event(event_id)
    assert after["state"] == prior
    assert after["disposition"] is None


def test_a_gap_event_never_reaches_reporting(api: Api) -> None:
    api.advance(900)
    [gap] = api.events(type="monitoring_gap")
    response = api.report(gap["event_id"])
    assert response.status_code == 409, response.text
    assert response.json() == NO_DISPOSITION
    assert api.event(gap["event_id"])["state"] == "FLAGGED"


def test_an_unknown_event_is_404(api: Api) -> None:
    response = api.report("evt-00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404, response.text
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("tier", ["OTHER_SERIOUS", "NOT_SERIOUS"])
def test_after_a_disposition_the_guard_passes_to_the_phase_5_report(api: Api, tier: str) -> None:
    event_id, _ = api.flag()
    rationale = "no person was affected" if tier == "NOT_SERIOUS" else None
    api.disposed(event_id, tier, rationale=rationale)
    before = api.entries()
    response = api.report(event_id)
    assert response.status_code == 501, response.text
    assert response.json() == NOT_IMPLEMENTED
    assert api.entries() == before
