"""AC-13: the report has the REQ-12 content, matches the proposed schema, and carries the
Article 73 template mapping (Phase 5 spec 3.3, 3.3.3, 3.3.4, 3.3.5)."""

import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pypdf import PdfReader
from sqlalchemy import Engine

from app import report_template
from app.report_render import BANNER
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.reports.conftest import SERIOUS, decided, disposed_event, fetch, get_report
from tests.worker.conftest import FakeClock

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "docs/contract/phase5/report.schema.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = Draft202012Validator(SCHEMA)
HARM = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)


def time(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def confirmed(*keys: str) -> dict[str, Any]:
    fields = decided()
    for key in keys:
        fields[key] = {"status": "CONFIRMED", "evidence": f"evidence for {key}"}
    return fields


def entry(report: dict[str, Any], field: str) -> dict[str, Any]:
    [found] = [f for f in report["article73_template"]["fields"] if f["field"] == field]
    result: dict[str, Any] = found
    return result


def pdf_pages(data: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(BytesIO(data)).pages]


@pytest.mark.parametrize("tier", [*SERIOUS, "NOT_SERIOUS"])
def test_ac13_report_matches_the_schema(
    client: ApiClient, fake_clock: FakeClock, tier: str
) -> None:
    event_id = disposed_event(client, fake_clock, tier)
    fake_clock.advance(3600)
    report = get_report(client, event_id)
    assert sorted(VALIDATOR.iter_errors(report), key=str) == []
    assert report["event"]["event_id"] == event_id
    # Four decided harm fields, each CONFIRMED or NOT_APPLICABLE with who and when.
    assert list(report["harm_fields"]) == list(HARM)
    for field in report["harm_fields"].values():
        assert field["status"] in ("CONFIRMED", "NOT_APPLICABLE")
        assert field["confirmed_by"] == "rev-1" and field["confirmed_at"]


def test_the_schema_rejects_a_report_missing_a_required_part(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    """The check is not vacuous: each REQ-12 part, removed or nulled, is an error."""
    event_id = disposed_event(client, fake_clock, "DEATH")
    report = get_report(client, event_id)
    for path in (
        ("event",),
        ("harm_fields", "property_environmental_damage"),
        ("determination", "tier"),
        ("timestamps", "detected_at"),
        ("timestamps", "determined_at"),
        ("timestamps", "assembled_at"),
    ):
        broken = json.loads(json.dumps(report))
        node = broken
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
        assert list(VALIDATOR.iter_errors(broken)), path
    for path in (("timestamps", "assembled_at"), ("determination", "reviewer_id")):
        broken = json.loads(json.dumps(report))
        broken[path[0]][path[1]] = None
        assert list(VALIDATOR.iter_errors(broken)), path


def test_ac13_three_timestamps_are_non_null_and_distinct(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    """Flag at T, advance 4 h, dispose, advance 1 h, report (spec 10.1)."""
    event_id = disposed_event(client, fake_clock, "DEATH", hours=4)
    fake_clock.advance(3600)
    stamps = get_report(client, event_id)["timestamps"]
    assert stamps["detected_at"] == "2026-09-27T10:05:00Z"
    assert stamps["determined_at"] == "2026-09-27T14:05:00Z"
    assert stamps["assembled_at"] == "2026-09-27T15:05:00Z"
    assert (
        time(stamps["detected_at"]) < time(stamps["determined_at"]) < time(stamps["assembled_at"])
    )
    assert stamps["last_revised_at"] is None


def test_report_timestamps_keep_their_fraction(client: ApiClient, fake_clock: FakeClock) -> None:
    """Q-3: distinct values at microsecond resolution are enough, so a report requested 250 ms
    after the disposition still has assembled_at after determined_at."""
    event_id = disposed_event(client, fake_clock, "DEATH", hours=4)
    fake_clock.advance(0.25)
    stamps = get_report(client, event_id)["timestamps"]
    assert stamps["determined_at"] == "2026-09-27T14:05:00Z"
    assert stamps["assembled_at"] == "2026-09-27T14:05:00.25Z"
    assert time(stamps["determined_at"]) < time(stamps["assembled_at"])


def test_the_determination_and_deadline_come_from_the_dispositions(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "OTHER_SERIOUS", hours=4)
    fake_clock.advance(86400)
    response = client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers=bearer("rev-1"),
        json={"tier": "DEATH", "rationale": None, "harm_fields": decided()},
    )
    assert response.status_code == 200, response.text
    fake_clock.advance(3600)
    report = get_report(client, event_id)
    determination = report["determination"]
    assert determination["tier"] == "DEATH"
    assert determination["tier_label"] == "Death"
    # The deadline is the first assignment (T0 + 4 h) plus the DEATH window of 10 days.
    assert determination["assigned_at"] == "2026-09-27T14:05:00Z"
    assert determination["deadline_utc"] == "2026-10-07T14:05:00Z"
    assert determination["calendar"] == "calendar_days"
    assert [(r["prior_tier"], r["new_tier"]) for r in determination["revisions"]] == [
        ("OTHER_SERIOUS", "DEATH")
    ]
    assert report["timestamps"]["determined_at"] == "2026-09-27T14:05:00Z"
    assert report["timestamps"]["last_revised_at"] == "2026-09-28T14:05:00Z"
    assert report["regulation"]["reporting_window_days"] == 10
    assert report["risk_indicator"] == {"detection_to_assignment_seconds": 4 * 3600}


@pytest.mark.parametrize(
    ("tier", "label", "window"),
    [
        ("DEATH", "Death", 10),
        (
            "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
            "Critical infrastructure disruption / widespread infringement",
            2,
        ),
        ("OTHER_SERIOUS", "Other serious incident", 15),
    ],
)
def test_each_serious_tier_gives_its_label_and_reporting_window(
    client: ApiClient, fake_clock: FakeClock, tier: str, label: str, window: int
) -> None:
    event_id = disposed_event(client, fake_clock, tier)
    fake_clock.advance(60)
    report = get_report(client, event_id)
    assert report["determination"]["tier_label"] == label
    assert report["regulation"]["reporting_window_days"] == window


def test_closure_report_for_not_serious_has_no_deadline(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "NOT_SERIOUS", rationale="seasonal shift")
    fake_clock.advance(3600)
    report = get_report(client, event_id)
    assert report["report_kind"] == "NOT_SERIOUS_CLOSURE"
    assert report["determination"]["deadline_utc"] is None
    assert report["regulation"]["reporting_window_days"] is None
    assert report["determination"]["rationale"] == "seasonal shift"
    assert report["determination"]["tier"] == "NOT_SERIOUS"


# The Article 73 template mapping (spec 3.3.4).


@pytest.mark.parametrize("tier", SERIOUS)
def test_report_carries_the_article73_template_mapping(
    client: ApiClient, fake_clock: FakeClock, tier: str
) -> None:
    event_id = disposed_event(client, fake_clock, tier, harm_fields=confirmed("health_safety_harm"))
    fake_clock.advance(3600)
    report = get_report(client, event_id)
    template = report["article73_template"]
    assert template["template_version"] == "1.0.0"
    assert template["template_reference"] == "SB-10407"
    assert template["template"] == (
        "Incident Report for Serious Incidents under the AI Act (High-risk AI systems)"
    )
    fields = template["fields"]
    # One entry per row of the mapping, in template order, with the exact name and status.
    assert [(f["field"], f["name"], f["status"]) for f in fields] == [
        (row.field, row.name, row.status) for row in report_template.ROWS
    ]
    for f in fields:
        if f["status"] in ("not_available", "requires_reviewer_input", "section", "group"):
            assert f["value"] is None, f["field"]
    # Spot checks against the template's own spelling (curly apostrophes included).
    assert entry(report, "1.2 c") == {
        "field": "1.2 c",
        "name": "Date and time of detection of the incident",
        "status": "mapped",
        "value": report["timestamps"]["detected_at"],
    }
    assert entry(report, "1.3.2 b")["name"] == "Contact’s first name"
    assert entry(report, "1.3.2 b")["status"] == "not_available"
    assert entry(report, "1.1 b")["status"] == "requires_reviewer_input"
    assert entry(report, "1.2 b")["value"] == "2026-09-27"
    # 1.2 d is the first serious-tier assignment, and equals determined_at here.
    assert entry(report, "1.2 d")["value"] == report["timestamps"]["determined_at"]
    assert entry(report, "4.2 b")["status"] == "not_available"
    assert entry(report, "4.2 b")["value"] is None
    description = entry(report, "3.1 a")["value"]
    assert description["likely_causality"] is None
    assert [e["field"] for e in description["effects"]] == ["health_safety_harm"]
    assert "accuracy_drift" in description["what_went_wrong"]
    provenance = entry(report, "§5")["value"]
    assert provenance["report_id"] == report["report_id"]
    assert provenance["ledger_anchor"] == report["ledger_anchor"]


@pytest.mark.parametrize(
    ("tier", "confirmed_keys", "expected"),
    [
        ("DEATH", (), ["Death"]),
        ("DEATH", ("health_safety_harm",), ["Death"]),
        (
            "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
            (),
            [report_template.INFRA_MANAGEMENT, report_template.INFRA_OPERATION],
        ),
        (
            "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
            ("fundamental_rights_infringement",),
            [
                report_template.INFRA_MANAGEMENT,
                report_template.INFRA_OPERATION,
                report_template.RIGHTS,
            ],
        ),
        ("OTHER_SERIOUS", (), ["All other reportable incidents"]),
        ("OTHER_SERIOUS", ("health_safety_harm",), ["Harm to a person’s health"]),
        (
            "OTHER_SERIOUS",
            ("property_environmental_damage", "fundamental_rights_infringement"),
            [report_template.RIGHTS, "Harm to property", "Harm to environment"],
        ),
        (
            "OTHER_SERIOUS",
            ("critical_infrastructure_disruption",),
            [report_template.INFRA_MANAGEMENT, report_template.INFRA_OPERATION],
        ),
    ],
)
def test_the_classification_candidates_follow_the_tier_and_confirmed_fields(
    client: ApiClient,
    fake_clock: FakeClock,
    tier: str,
    confirmed_keys: tuple[str, ...],
    expected: list[str],
) -> None:
    event_id = disposed_event(client, fake_clock, tier, harm_fields=confirmed(*confirmed_keys))
    fake_clock.advance(60)
    report = get_report(client, event_id)
    assert entry(report, "1.2 g")["value"] == expected
    assert entry(report, "1.2 g")["status"] == "derivable"


def test_the_closure_report_omits_the_fields_that_do_not_apply(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "NOT_SERIOUS", rationale="seasonal shift")
    fake_clock.advance(60)
    report = get_report(client, event_id)
    present = {f["field"] for f in report["article73_template"]["fields"]}
    for absent in ("1.1 a", "1.1 b", "1.2 a", "1.2 d", "1.2 e", "1.2 f", "1.2 g", "3.2 a", "3.2 b"):
        assert absent not in present
    for absent in ("3.2 c", "4.1 a", "4.1 B", "4.1 c", "4.2 a", "4.2 c", "Declaration"):
        assert absent not in present
    for kept in ("1.2 b", "1.2 c", "3.1 a", "4.2 b", "§5", "1.3.2 a", "2.2 a"):
        assert kept in present
    rationale = entry(report, "4.2 b")
    assert (rationale["status"], rationale["value"]) == ("mapped", "seasonal shift")


def test_closure_report_is_marked_not_for_submission(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    closure = disposed_event(client, fake_clock, "NOT_SERIOUS", rationale="seasonal shift")
    fake_clock.advance(60)
    report = get_report(client, closure)
    assert report["submission_marking"] == "NOT FOR SUBMISSION"
    pages = pdf_pages(fetch(client, closure, "pdf").content)
    assert pages
    assert all(BANNER in page for page in pages)


def test_a_serious_report_has_no_marking_and_no_banner(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    serious = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(60)
    assert get_report(client, serious)["submission_marking"] is None
    assert all(BANNER not in page for page in pdf_pages(fetch(client, serious, "pdf").content))


def test_the_report_json_is_one_schema_for_gap_free_breach_events_only(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine
) -> None:
    """A gap event never reaches a report: 409 NO_DISPOSITION (spec 3.3.3, 3.6 step 6)."""
    from tests.part2.conftest import add_gap_event

    gap = add_gap_event(owner_engine)
    response = fetch(client, gap)
    assert (response.status_code, response.json()["error_code"]) == (409, "NO_DISPOSITION")
