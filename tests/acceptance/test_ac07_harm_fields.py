"""AC-07 (REQ-07): a flagged event's four Article 3(49) fields are pre-filled from the evidence
available, the field with none is blank rather than guessed, no field carries a severity, and
each field's confirmation and confirming reviewer is recorded.

Spec Part 1 1.3 (the template, pinned) and README 10.3: with the one seeded harm_context row
(fundamental_rights_infringement), accuracy 0.61 against the 0.70 limit of thr-2 gives

  accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at {detected_at}. {context}

and the other three fields have null evidence. Confirmation travels in the disposition call's
harm_fields (D3); spec 1.5 says who and when is recorded. "Retrieve the generated
parameterized report before reviewer action" is the report guard of spec 2.8: 409.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance.api import (
    HARM_FIELDS_UNCONFIRMED,
    HARM_KEYS,
    NO_DISPOSITION,
    RIGHTS_CONTEXT,
    Api,
    api_time,
    decisions,
)

pytestmark = pytest.mark.integration

RIGHTS = "fundamental_rights_infringement"


def _expected_evidence(detected_at: str) -> str:
    return (
        f"accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at {detected_at}. "
        f"{RIGHTS_CONTEXT}"
    )


def test_fields_with_evidence_are_prefilled_and_the_others_are_blank(api: Api) -> None:
    event_id, detected = api.flag("0.61")
    event = api.event(event_id)
    assert event["detected_at"] == api_time(detected)

    fields = event["harm_fields"]
    assert list(fields) == list(HARM_KEYS)
    assert fields[RIGHTS] == {
        "status": "PREFILLED_PENDING_CONFIRMATION",
        "evidence": _expected_evidence(event["detected_at"]),
        "confirmed_by": None,
        "confirmed_at": None,
    }
    for key in HARM_KEYS:
        if key != RIGHTS:
            assert fields[key] == {
                "status": "PREFILLED_PENDING_CONFIRMATION",
                "evidence": None,
                "confirmed_by": None,
                "confirmed_at": None,
            }, key
    # No system-assigned severity.
    assert event["disposition"] is None
    assert event["deadline_utc"] is None
    assert event["revisions"] == []


def test_the_report_before_reviewer_action_is_refused(api: Api) -> None:
    event_id, _ = api.flag("0.61")
    response = api.report(event_id)
    assert response.status_code == 409, response.text
    assert response.json() == NO_DISPOSITION


def test_a_field_left_pending_is_refused_and_nothing_is_recorded(api: Api) -> None:
    event_id, _ = api.flag("0.61")
    before = api.entries()
    pending: dict[str, Any] = decisions()
    pending["critical_infrastructure_disruption"] = {
        "status": "PREFILLED_PENDING_CONFIRMATION",
        "evidence": None,
    }
    response = api.dispose(event_id, "OTHER_SERIOUS", harm_fields=pending)
    assert response.status_code == 409, response.text
    assert response.json() == HARM_FIELDS_UNCONFIRMED
    assert api.entries() == before
    assert api.event(event_id)["harm_fields"][RIGHTS]["confirmed_by"] is None


def test_each_confirmation_records_the_reviewer_and_the_time(api: Api) -> None:
    event_id, _ = api.flag("0.61")
    detected_at = api.event(event_id)["detected_at"]
    fields = decisions(
        critical_infrastructure_disruption={
            "status": "CONFIRMED",
            "evidence": "routing outputs were affected",
        }
    )
    event = api.disposed(event_id, "OTHER_SERIOUS", harm_fields=fields)
    assigned_at = event["disposition"]["assigned_at"]

    stored = event["harm_fields"]
    # CONFIRMED with evidence null keeps the pre-fill (spec 1.5).
    assert stored[RIGHTS] == {
        "status": "CONFIRMED",
        "evidence": _expected_evidence(detected_at),
        "confirmed_by": "rev-1",
        "confirmed_at": assigned_at,
    }
    assert stored["critical_infrastructure_disruption"] == {
        "status": "CONFIRMED",
        "evidence": "routing outputs were affected",
        "confirmed_by": "rev-1",
        "confirmed_at": assigned_at,
    }
    for key in ("health_safety_harm", "property_environmental_damage"):
        assert stored[key] == {
            "status": "NOT_APPLICABLE",
            "evidence": None,
            "confirmed_by": "rev-1",
            "confirmed_at": assigned_at,
        }
    # The record is what a later read shows, too.
    assert api.event(event_id)["harm_fields"] == stored
