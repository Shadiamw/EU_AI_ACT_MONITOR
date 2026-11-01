"""AC-09 (REQ-08b): exactly four disposition options; "Assessed - not a serious incident"
without a rationale is rejected; with one, the event closes with no deadline started.

Contract: tier is one of DEATH, CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD, OTHER_SERIOUS,
NOT_SERIOUS; Example 3 is the exact 400 body. Spec 2.3: anything else is 400 MALFORMED_BODY;
a blank rationale is absent, null or whitespace only. Spec 5: NOT_SERIOUS -> CLOSED with
deadline_utc null. Spec 7: RATIONALE_REQUIRED is checked before the event lookup.
The options as presented on screen are the manual half (README 13, console).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance.api import (
    HARM_KEYS,
    MALFORMED,
    RATIONALE_REQUIRED,
    TIER_DAYS,
    TIERS,
    Api,
    parse_time,
)

pytestmark = pytest.mark.integration

ALL_NOT_APPLICABLE: dict[str, Any] = {
    key: {"status": "NOT_APPLICABLE", "evidence": None} for key in HARM_KEYS
}


@pytest.mark.parametrize("tier", TIERS)
def test_each_of_the_four_options_is_accepted(api: Api, tier: str) -> None:
    event_id, _ = api.flag()
    rationale = "assessed: no harm to any person" if tier == "NOT_SERIOUS" else None
    event = api.disposed(event_id, tier, rationale=rationale)
    assert event["disposition"]["tier"] == tier
    if tier == "NOT_SERIOUS":
        assert event["state"] == "CLOSED"
        assert event["deadline_utc"] is None
    else:
        assert event["state"] == "DISPOSITIONED"
        window = parse_time(event["deadline_utc"]) - parse_time(event["disposition"]["assigned_at"])
        assert window.total_seconds() == TIER_DAYS[tier] * 86400


@pytest.mark.parametrize(
    "tier",
    ["SERIOUS", "death", "Death", "OTHER", "NOT_A_SERIOUS_INCIDENT", "", None, 1],
)
def test_a_fifth_option_is_malformed_and_changes_nothing(api: Api, tier: Any) -> None:
    event_id, _ = api.flag()
    before = api.entries()
    response = api.dispose(event_id, tier)
    assert response.status_code == 400, response.text
    assert response.json() == MALFORMED
    assert api.entries() == before
    assert api.event(event_id)["disposition"] is None


@pytest.mark.parametrize("rationale", [None, "", "   \t\n"], ids=["null", "empty", "whitespace"])
def test_not_serious_without_a_rationale_is_contract_example_3(
    api: Api, rationale: str | None
) -> None:
    event_id, _ = api.flag()
    state = api.event(event_id)["state"]
    before = api.entries()
    response = api.dispose(
        event_id, "NOT_SERIOUS", rationale=rationale, harm_fields=ALL_NOT_APPLICABLE
    )
    assert response.status_code == 400, response.text
    assert response.json() == RATIONALE_REQUIRED
    assert api.entries() == before
    after = api.event(event_id)
    assert after["state"] == state
    assert after["disposition"] is None


def test_not_serious_with_the_rationale_omitted_is_example_3(api: Api) -> None:
    """Example 3's request carries no rationale key at all."""
    event_id, _ = api.flag()
    response = api.dispose(
        event_id, "NOT_SERIOUS", harm_fields=ALL_NOT_APPLICABLE, omit=("rationale",)
    )
    assert response.status_code == 400, response.text
    assert response.json() == RATIONALE_REQUIRED


def test_the_rationale_check_comes_before_the_event_lookup(api: Api) -> None:
    response = api.dispose(
        "evt-00000000-0000-0000-0000-000000000000",
        "NOT_SERIOUS",
        harm_fields=ALL_NOT_APPLICABLE,
    )
    assert response.status_code == 400, response.text
    assert response.json() == RATIONALE_REQUIRED


def test_not_serious_with_a_rationale_closes_with_no_deadline(api: Api) -> None:
    event_id, _ = api.flag()
    event = api.disposed(
        event_id,
        "NOT_SERIOUS",
        rationale="  sensor recalibration, no decision affected  ",
        harm_fields=ALL_NOT_APPLICABLE,
    )
    assert event["state"] == "CLOSED"
    assert event["deadline_utc"] is None
    assert event["disposition"]["tier"] == "NOT_SERIOUS"
    assert event["disposition"]["rationale"] == "sensor recalibration, no decision affected"
    assert event["disposition"]["reviewer_id"] == "rev-1"
    assert event["revisions"] == []
    assert api.event(event_id)["state"] == "CLOSED"
