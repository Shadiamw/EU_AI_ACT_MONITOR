"""Phase 4 Part 2: POST /v1/events/{event_id}/disposition and the report guard, through the API
(spec 2.3 to 2.11, Addendum rules 1 and 6). Clock: the fake clock (FakeClock) throughout; no
test endpoint is called. escalations.plan is Part 3's base-commit stub here, so nothing about
reminders is asserted (those tests are in test_disposition_integration.py).
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import escalations, ledger, review
from app.ledger import verify_ledger
from app.main import create_app
from tests.api.conftest import EVENT_KEYS, FORBIDDEN, MALFORMED, NOT_FOUND, UNAUTHORIZED, ApiClient
from tests.db_helpers import HARM_FIELDS, bearer
from tests.part1.conftest import T0, get, rows
from tests.part2.conftest import (
    ANCHOR,
    EXTENSION,
    NO_DISPOSITION,
    NOT_IMPLEMENTED,
    NOT_REVIEWABLE,
    RATIONALE_REQUIRED,
    SERIOUS,
    TIERS,
    UNCONFIRMED,
    add_gap_event,
    body,
    decided,
    dispose,
    disposition_rows,
    event_row,
    flag,
    ledger_rows,
    parse,
    report,
    snapshot,
)
from tests.worker.conftest import FakeClock

HEALTH, INFRASTRUCTURE, RIGHTS, PROPERTY = HARM_FIELDS
ALL_NOT_APPLICABLE = dict.fromkeys(HARM_FIELDS, "NOT_APPLICABLE")
DAY = timedelta(days=1)


def at(moment: str) -> datetime:
    return parse(moment)


def assigned(fake_clock: FakeClock, client: ApiClient, tier: str, **kwargs: Any) -> str:
    """Flag contract Example 1 at T0, move the clock to ANCHOR and record the first disposition."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    response = dispose(client, event_id, tier, **kwargs)
    assert response.status_code == 200, response.text
    return event_id


def ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def detail(client: ApiClient, event_id: str) -> dict[str, Any]:
    """The Event through the viewer key, which never changes state (spec 1.4)."""
    return ok(get(client, f"/v1/events/{event_id}"))


def new_entries(owner_engine: Engine, after: int) -> list[dict[str, Any]]:
    return rows(owner_engine, "SELECT * FROM ledger_entry WHERE seq > :s ORDER BY seq", s=after)


def head(owner_engine: Engine) -> int:
    return len(ledger_rows(owner_engine))


# The first disposition: contract Example 2 (spec 4, 2.4, 2.7, 2.10).


def test_a_first_disposition_records_contract_example_2(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """OTHER_SERIOUS assigned at 2026-09-27T10:12:00Z gives deadline_utc 2026-10-12T10:12:00Z,
    exactly 15 calendar days (contract Example 2). The response is the full Event, the same as
    the detail; one ledger entry, DISPOSITION_RECORDED, and no EVENT_VIEWED."""
    event_id = flag(client)
    before = head(owner_engine)
    fake_clock.set(ANCHOR)
    rights = {"status": "CONFIRMED", "evidence": None}
    response = ok(
        dispose(client, event_id, "OTHER_SERIOUS", harm_fields={**decided(), RIGHTS: rights})
    )

    assert set(response) == EVENT_KEYS
    assert response["state"] == "DISPOSITIONED"
    assert response["disposition"] == {
        "tier": "OTHER_SERIOUS",
        "rationale": None,
        "reviewer_id": "rev-1",
        "assigned_at": "2026-09-27T10:12:00Z",
    }
    assert list(response["disposition"]) == ["tier", "rationale", "reviewer_id", "assigned_at"]
    assert response["deadline_utc"] == "2026-10-12T10:12:00Z"
    assert response["revisions"] == []
    assert response["detected_at"] == "2026-09-27T10:05:00Z"
    for key in HARM_FIELDS:
        field = response["harm_fields"][key]
        assert field["status"] == ("CONFIRMED" if key == RIGHTS else "NOT_APPLICABLE")
        assert (field["confirmed_by"], field["confirmed_at"]) == ("rev-1", "2026-09-27T10:12:00Z")

    [row] = disposition_rows(owner_engine, event_id)
    assert row["disposition_id"].startswith("dsp-")
    assert {k: v for k, v in row.items() if k != "disposition_id"} == {
        "event_id": event_id,
        "seq_in_event": 1,
        "tier": "OTHER_SERIOUS",
        "rationale": None,
        "reviewer_id": "rev-1",
        "assigned_at": ANCHOR,
        "anchor_at": ANCHOR,
        "calendar": "calendar_days",
        "deadline_utc": at("2026-10-12T10:12:00Z"),
        "supersedes_id": None,
    }
    stored = event_row(owner_engine, event_id)
    assert (stored["state"], stored["deadline_utc"]) == ("DISPOSITIONED", row["deadline_utc"])

    [entry] = new_entries(owner_engine, before)
    assert (entry["entry_type"], entry["actor_id"], entry["subject_id"]) == (
        "DISPOSITION_RECORDED",
        "rev-1",
        event_id,
    )
    assert entry["payload"] == {
        "event_id": event_id,
        "disposition_id": row["disposition_id"],
        "tier": "OTHER_SERIOUS",
        "rationale": None,
        "deadline_utc": "2026-10-12T10:12:00Z",
        "anchor_at": "2026-09-27T10:12:00Z",
        "calendar": "calendar_days",
        "harm_fields": {**ALL_NOT_APPLICABLE, RIGHTS: "CONFIRMED"},
    }
    # The response is the Event the detail shows (the detail's own view appends EVENT_VIEWED).
    assert detail(client, event_id) == response


def test_a_reviewers_view_then_a_disposition_moves_under_review_to_dispositioned(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Contract: FLAGGED/UNDER_REVIEW -> DISPOSITIONED. The other tests dispose straight from
    FLAGGED, so a reviewer who never opened the event is not blocked (spec 1.4)."""
    event_id = flag(client)
    assert ok(get(client, f"/v1/events/{event_id}", "rev-1"))["state"] == "UNDER_REVIEW"
    assert ok(dispose(client, event_id, "DEATH"))["state"] == "DISPOSITIONED"


# AC-09: exactly four options; NOT_SERIOUS needs a rationale (spec 2.3, 2.11).


@pytest.mark.parametrize(
    "rationale",
    [None, "", " ", "\t\n ", "　", "absent"],
    ids=["null", "empty", "space", "whitespace", "ideographic-space", "absent"],
)
def test_ac09_not_serious_without_a_rationale_is_contract_example_3(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, rationale: str | None
) -> None:
    """400 with exactly the Example 3 body; state unchanged, no disposition row, no ledger
    entry, no field change."""
    event_id = flag(client)
    before = snapshot(owner_engine, event_id)
    request = body("NOT_SERIOUS", rationale)
    if rationale == "absent":
        del request["rationale"]
    response = client.request(
        "POST", f"/v1/events/{event_id}/disposition", headers=bearer("rev-1"), json=request
    )
    assert (response.status_code, response.json()) == (400, RATIONALE_REQUIRED)
    assert snapshot(owner_engine, event_id) == before


def test_ac09_not_serious_with_a_rationale_closes_with_no_deadline(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    response = ok(dispose(client, event_id, "NOT_SERIOUS", "  seasonal shift, no user impact \n"))
    assert response["state"] == "CLOSED"
    assert response["deadline_utc"] is None
    assert response["disposition"] == {
        "tier": "NOT_SERIOUS",
        "rationale": "seasonal shift, no user impact",
        "reviewer_id": "rev-1",
        "assigned_at": "2026-09-27T10:12:00Z",
    }
    [row] = disposition_rows(owner_engine, event_id)
    assert (row["rationale"], row["deadline_utc"]) == ("seasonal shift, no user impact", None)
    assert event_row(owner_engine, event_id)["deadline_utc"] is None
    entry = ledger_rows(owner_engine)[-1]
    assert entry["entry_type"] == "DISPOSITION_RECORDED"
    assert entry["payload"]["rationale"] == "seasonal shift, no user impact"
    assert entry["payload"]["deadline_utc"] is None


@pytest.mark.parametrize("tier", TIERS)
def test_ac09_each_of_the_four_tiers_is_accepted(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, tier: str
) -> None:
    event_id = flag(client)
    response = ok(dispose(client, event_id, tier, "a reviewer's reason"))
    assert response["disposition"]["tier"] == tier
    assert response["state"] == ("CLOSED" if tier == "NOT_SERIOUS" else "DISPOSITIONED")


@pytest.mark.parametrize(
    "tier",
    ["death", "SERIOUS", "", None, 3, "DEATH ", ["DEATH"], {"tier": "DEATH"}, True, "absent"],
    ids=repr,
)
def test_ac09_any_other_tier_is_malformed(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, tier: Any
) -> None:
    event_id = flag(client)
    before = snapshot(owner_engine, event_id)
    request = body("DEATH", "a reason")
    if tier == "absent":
        del request["tier"]
    else:
        request["tier"] = tier
    response = client.request(
        "POST", f"/v1/events/{event_id}/disposition", headers=bearer("rev-1"), json=request
    )
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert snapshot(owner_engine, event_id) == before


# Spec 2.3: the body's shape.

VALID = json.dumps(body("DEATH"))

MALFORMED_BODIES = {
    "not JSON": "{not json",
    "empty": "",
    "array": json.dumps([body("DEATH")]),
    "string": json.dumps("DEATH"),
    "null": "null",
    "rationale a number": json.dumps(body("DEATH", 7)),
    "rationale a list": json.dumps(body("DEATH", ["a"])),
    "rationale an object": json.dumps(body("DEATH", {"text": "a"})),
    "rationale 4,001 code points": json.dumps(body("DEATH", "r" * 4001)),
    "rationale with U+0000": json.dumps(body("DEATH", "a\u0000b")),
    "rationale with a lone surrogate": VALID.replace('"rationale": null', '"rationale": "\\ud800"'),
    "harm_fields a string": json.dumps(body("DEATH", None, "all fine")),
    "harm_fields an unknown key": json.dumps(
        body("DEATH", None, {**decided(), "other_harm": {"status": "CONFIRMED"}})
    ),
    "harm_fields a bad status": json.dumps(
        body("DEATH", None, {**decided(), HEALTH: {"status": "MAYBE", "evidence": None}})
    ),
}


@pytest.mark.parametrize("content", MALFORMED_BODIES.values(), ids=MALFORMED_BODIES.keys())
def test_a_malformed_body_is_400_and_writes_nothing(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, content: str
) -> None:
    event_id = flag(client)
    before = snapshot(owner_engine, event_id)
    response = client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers={**bearer("rev-1"), "Content-Type": "application/json"},
        content=content,
    )
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert snapshot(owner_engine, event_id) == before


def test_rationale_limits_and_unknown_top_level_keys(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """4,000 code points is accepted (counted before trimming, a character outside the BMP
    once); unknown top-level keys are ignored, as in POST /v1/metrics (spec 2.3)."""
    event_id = flag(client)
    longest = "\U0001f600" * 4000
    request = {**body("NOT_SERIOUS", longest), "priority": "high", "tier_hint": None}
    response = client.request(
        "POST", f"/v1/events/{event_id}/disposition", headers=bearer("rev-1"), json=request
    )
    assert ok(response)["disposition"]["rationale"] == longest


def test_a_blank_rationale_on_a_serious_tier_is_stored_as_null(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = flag(client)
    assert ok(dispose(client, event_id, "DEATH", "   "))["disposition"]["rationale"] is None
    assert disposition_rows(owner_engine, event_id)[0]["rationale"] is None


# The pinned order of checks (spec 7, 2.4; Addendum rule 2).


def test_the_order_of_checks(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """The first failing check in the pinned order decides the response: 401, 403, 400
    MALFORMED_BODY, 400 RATIONALE_REQUIRED, 404, 409 EVENT_NOT_REVIEWABLE, 409
    HARM_FIELDS_UNCONFIRMED, 409 DEADLINE_EXTENSION_REQUIRES_RATIONALE."""
    event_id = assigned(fake_clock, client, "DEATH")
    gap_id = add_gap_event(owner_engine)
    before = ledger_rows(owner_engine)

    def call(event: str, request: Any, actor: str | None = "rev-1") -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers.update(bearer(actor))
        content = request if isinstance(request, str) else json.dumps(request)
        response = client.request(
            "POST", f"/v1/events/{event}/disposition", headers=headers, content=content
        )
        return response.status_code, response.json()

    missing_harm = {"tier": "OTHER_SERIOUS", "rationale": None}
    malformed_not_serious = body("NOT_SERIOUS", None, "broken")
    # 401 before everything, 403 before the body.
    assert call("evt-nope", "{bad", actor=None) == (401, UNAUTHORIZED)
    assert call("evt-nope", "{bad", actor="viewer-1") == (403, FORBIDDEN)
    assert call("evt-nope", "{bad", actor="sys-42") == (403, FORBIDDEN)
    # 400 MALFORMED_BODY before RATIONALE_REQUIRED, and both before the event lookup.
    assert call("evt-nope", malformed_not_serious) == (400, MALFORMED)
    assert call("evt-nope", body("NOT_SERIOUS")) == (400, RATIONALE_REQUIRED)
    assert call(gap_id, body("NOT_SERIOUS")) == (400, RATIONALE_REQUIRED)
    # Addendum rule 2: missing harm fields for an unknown event is 404, for a gap event 409
    # EVENT_NOT_REVIEWABLE.
    assert call("evt-nope", missing_harm) == (404, NOT_FOUND)
    assert call(gap_id, missing_harm) == (409, NOT_REVIEWABLE)
    # A looser tier with incomplete fields: HARM_FIELDS_UNCONFIRMED comes first.
    assert call(event_id, missing_harm) == (409, UNCONFIRMED)
    assert call(event_id, body("OTHER_SERIOUS")) == (409, EXTENSION)
    assert ledger_rows(owner_engine) == before


# AC-08 and the report guard (spec 2.8, D1, Addendum rule 6).


def test_ac08_a_report_without_a_disposition_is_409_and_changes_nothing(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = flag(client)
    before = snapshot(owner_engine, event_id)
    response = report(client, event_id)
    assert (response.status_code, response.json()) == (409, NO_DISPOSITION)
    assert snapshot(owner_engine, event_id) == before
    assert before["event"]["state"] == "FLAGGED"

    # UNDER_REVIEW through the reviewer's first view (Part 1, D3).
    assert ok(get(client, f"/v1/events/{event_id}", "rev-1"))["state"] == "UNDER_REVIEW"
    before = snapshot(owner_engine, event_id)
    response = report(client, event_id)
    assert (response.status_code, response.json()) == (409, NO_DISPOSITION)
    assert snapshot(owner_engine, event_id) == before
    assert before["event"]["state"] == "UNDER_REVIEW"


@pytest.mark.parametrize("tier", TIERS)
def test_a_report_after_any_disposition_is_501_until_phase_5(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, tier: str
) -> None:
    event_id = flag(client)
    ok(dispose(client, event_id, tier, "reason"))
    before = snapshot(owner_engine, event_id)
    response = report(client, event_id)
    assert (response.status_code, response.json()) == (501, NOT_IMPLEMENTED)
    assert snapshot(owner_engine, event_id) == before


def test_a_report_on_an_unknown_event_is_404(client: ApiClient, owner_engine: Engine) -> None:
    before = ledger_rows(owner_engine)
    response = report(client, "evt-nope")
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert ledger_rows(owner_engine) == before


# Gap events (INV-12).


def test_a_gap_event_cannot_be_dispositioned_and_has_no_report(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    gap_id = add_gap_event(owner_engine)
    before = snapshot(owner_engine, gap_id)
    for tier in TIERS:
        response = dispose(client, gap_id, tier, "a reason")
        assert (response.status_code, response.json()) == (409, NOT_REVIEWABLE)
    response = report(client, gap_id)
    assert (response.status_code, response.json()) == (409, NO_DISPOSITION)
    assert snapshot(owner_engine, gap_id) == before
    assert before["event"]["state"] == "FLAGGED"
    assert before["event"]["deadline_utc"] is None


# AC-14: reviewer only, on both actions (REQ-13; Addendum rule 6).


@pytest.mark.parametrize("actor", ["viewer-1", "sys-42", None])
def test_ac14_non_reviewers_are_denied_on_both_actions(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, actor: str | None
) -> None:
    event_id = flag(client)
    expected = (401, UNAUTHORIZED) if actor is None else (403, FORBIDDEN)
    for tier in TIERS:
        before = snapshot(owner_engine, event_id)
        response = dispose(client, event_id, tier, "reason", actor=actor)
        assert (response.status_code, response.json()) == expected
        assert snapshot(owner_engine, event_id) == before
    before = snapshot(owner_engine, event_id)
    response = report(client, event_id, actor)
    assert (response.status_code, response.json()) == expected
    # After a disposition the denial is the same.
    ok(dispose(client, event_id, "DEATH"))
    before = snapshot(owner_engine, event_id)
    for response in (
        dispose(client, event_id, "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", actor=actor),
        report(client, event_id, actor),
    ):
        assert (response.status_code, response.json()) == expected
    assert snapshot(owner_engine, event_id) == before


def test_ac14_the_reviewer_succeeds_on_both_actions(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Disposition 200; report 409 before a disposition and 501 after one: the reviewer passes
    the role check (D1)."""
    event_id = flag(client)
    response = report(client, event_id)
    assert (response.status_code, response.json()) == (409, NO_DISPOSITION)
    ok(dispose(client, event_id, "DEATH"))
    response = report(client, event_id)
    assert (response.status_code, response.json()) == (501, NOT_IMPLEMENTED)


# AC-15 edit step: a view by one user and an edit by another, each logged with identity and time.


def test_ac15_a_view_by_the_viewer_and_an_edit_by_the_reviewer_are_both_audited(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = flag(client)
    fake_clock.set(datetime(2026, 9, 27, 10, 8, tzinfo=UTC))
    get(client, f"/v1/events/{event_id}", "viewer-1")
    fake_clock.set(ANCHOR)
    ok(dispose(client, event_id, "OTHER_SERIOUS"))
    trail = get(client, f"/v1/events/{event_id}/audit", "viewer-1").json()
    assert {
        "actor_id": "viewer-1",
        "action": "EVENT_VIEWED",
        "timestamp": "2026-09-27T10:08:00.000Z",
    } in trail
    assert {
        "actor_id": "rev-1",
        "action": "DISPOSITION_RECORDED",
        "timestamp": "2026-09-27T10:12:00.000Z",
    } in trail
    actions = [entry["action"] for entry in trail]
    assert actions.index("EVENT_VIEWED") < actions.index("DISPOSITION_RECORDED")


# AC-10: revisions keep the history (REQ-09, INV-4).


def test_ac10_a_revision_appends_a_row_and_keeps_the_original(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = assigned(fake_clock, client, "OTHER_SERIOUS")
    [first] = disposition_rows(owner_engine, event_id)
    before = head(owner_engine)

    fake_clock.set(ANCHOR + 3 * DAY)
    response = ok(dispose(client, event_id, "DEATH"))
    assert response["revisions"] == [
        {
            "prior_tier": "OTHER_SERIOUS",
            "new_tier": "DEATH",
            "reviewer_id": "rev-1",
            "revised_at": "2026-09-30T10:12:00Z",
        }
    ]
    assert list(response["revisions"][0]) == ["prior_tier", "new_tier", "reviewer_id", "revised_at"]
    # tier is the decision in force; reviewer and time are the first assignment's.
    assert response["disposition"] == {
        "tier": "DEATH",
        "rationale": None,
        "reviewer_id": "rev-1",
        "assigned_at": "2026-09-27T10:12:00Z",
    }
    old, new = disposition_rows(owner_engine, event_id)
    assert old == first  # the original, unchanged in SQL
    assert (new["seq_in_event"], new["supersedes_id"]) == (2, first["disposition_id"])
    assert (new["tier"], new["assigned_at"], new["anchor_at"]) == (
        "DEATH",
        ANCHOR + 3 * DAY,
        ANCHOR,
    )
    [entry] = new_entries(owner_engine, before)
    assert (entry["entry_type"], entry["actor_id"], entry["subject_id"]) == (
        "DISPOSITION_REVISED",
        "rev-1",
        event_id,
    )
    assert entry["payload"] == {
        "event_id": event_id,
        "disposition_id": new["disposition_id"],
        "supersedes_id": first["disposition_id"],
        "prior_tier": "OTHER_SERIOUS",
        "new_tier": "DEATH",
        "rationale": None,
        "prior_deadline_utc": "2026-10-12T10:12:00Z",
        "deadline_utc": "2026-10-07T10:12:00Z",
        "anchor_at": "2026-09-27T10:12:00Z",
        "calendar": "calendar_days",
        "harm_fields": ALL_NOT_APPLICABLE,
        "harm_fields_changed": [],
    }
    actions = [e["action"] for e in get(client, f"/v1/events/{event_id}/audit").json()]
    assert "DISPOSITION_RECORDED" in actions
    assert "DISPOSITION_REVISED" in actions

    # Append-only as the application role, even for statements that match nothing.
    for statement in (
        "UPDATE disposition SET tier = 'NOT_SERIOUS'",
        "UPDATE disposition SET rationale = 'x' WHERE false",
        "DELETE FROM disposition",
        "DELETE FROM disposition WHERE false",
    ):
        with pytest.raises(DBAPIError), app_engine.begin() as conn:
            conn.execute(text(statement))
    assert disposition_rows(owner_engine, event_id) == [old, new]


def test_harm_fields_changed_lists_the_keys_a_revision_changed(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = assigned(fake_clock, client, "DEATH")
    changed = {
        **decided(),
        PROPERTY: {"status": "CONFIRMED", "evidence": "a server room flooded"},
        HEALTH: {"status": "CONFIRMED", "evidence": "x"},
    }
    ok(dispose(client, event_id, "DEATH", harm_fields=changed))
    payload = ledger_rows(owner_engine)[-1]["payload"]
    assert payload["harm_fields_changed"] == [HEALTH, PROPERTY]
    assert payload["harm_fields"] == {
        **ALL_NOT_APPLICABLE,
        HEALTH: "CONFIRMED",
        PROPERTY: "CONFIRMED",
    }
    assert "a server room flooded" not in json.dumps(payload)  # no evidence text in the ledger


# AC-11: deadlines from the assignment, and the revision rules (spec 2.5, 6).


@pytest.mark.parametrize("tier", SERIOUS)
def test_ac11_the_deadline_runs_from_the_assignment_not_the_detection(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, tier: str
) -> None:
    """Flag, advance 4 h, assign: deadline_utc - assigned_at is exactly 2, 10 or 15 days."""
    days = {"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "DEATH": 10, "OTHER_SERIOUS": 15}[tier]
    event_id = flag(client)
    fake_clock.advance(14_400)
    response = ok(dispose(client, event_id, tier))
    assigned_at = parse(response["disposition"]["assigned_at"])
    assert assigned_at == T0 + timedelta(hours=4)
    assert parse(response["deadline_utc"]) - assigned_at == timedelta(days=days)
    assert parse(response["deadline_utc"]) != parse(response["detected_at"]) + timedelta(days=days)
    assert response["detected_at"] == "2026-09-27T10:05:00Z"


# Spec 2.5, the worked examples through the API, plus DEATH -> CRITICAL and a same-tier revision.
REVISIONS = {
    "stricter": ("OTHER_SERIOUS", "DEATH", None, 200, "2026-10-07T10:12:00Z"),
    "looser without rationale": ("DEATH", "OTHER_SERIOUS", None, 409, "2026-10-07T10:12:00Z"),
    "looser with rationale": (
        "DEATH",
        "OTHER_SERIOUS",
        "the death was unrelated",
        200,
        "2026-10-12T10:12:00Z",
    ),
    "death to critical": (
        "DEATH",
        "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
        None,
        200,
        "2026-09-29T10:12:00Z",
    ),
    "same tier": ("DEATH", "DEATH", None, 200, "2026-10-07T10:12:00Z"),
    "critical to death without rationale": (
        "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
        "DEATH",
        None,
        409,
        "2026-09-29T10:12:00Z",
    ),
}


@pytest.mark.parametrize(
    ("first", "second", "rationale", "status", "deadline"),
    REVISIONS.values(),
    ids=REVISIONS.keys(),
)
def test_ac11_revisions(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    first: str,
    second: str,
    rationale: str | None,
    status: int,
    deadline: str,
) -> None:
    """The first tier at 2026-09-27T10:12:00Z, the revision three days later; the deadline is
    always from the original assignment."""
    event_id = assigned(fake_clock, client, first)
    fake_clock.set(ANCHOR + 3 * DAY)
    before = snapshot(owner_engine, event_id)
    response = dispose(client, event_id, second, rationale)
    assert response.status_code == status
    if status == 409:
        assert response.json() == EXTENSION
        assert snapshot(owner_engine, event_id) == before
    else:
        assert response.json()["revisions"][-1]["prior_tier"] == first
        assert response.json()["revisions"][-1]["new_tier"] == second
        assert response.json()["disposition"]["rationale"] == rationale
        latest = disposition_rows(owner_engine, event_id)[-1]
        assert latest["rationale"] == rationale
        assert ledger_rows(owner_engine)[-1]["payload"]["rationale"] == rationale
    assert detail(client, event_id)["deadline_utc"] == deadline
    assert event_row(owner_engine, event_id)["deadline_utc"] == at(deadline)


def test_working_days_are_recorded_at_the_first_disposition(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 2.5 working-days example through the API: Sunday anchor, OTHER_SERIOUS, no
    holidays, 2026-10-16T10:12:00Z; the calendar is recorded on the row."""
    monkeypatch.setenv("DEADLINE_CALENDAR", "working_days")
    event_id = assigned(fake_clock, client, "OTHER_SERIOUS")
    assert detail(client, event_id)["deadline_utc"] == "2026-10-16T10:12:00Z"
    assert disposition_rows(owner_engine, event_id)[0]["calendar"] == "working_days;holidays="


def test_a_revision_keeps_the_recorded_calendar_after_a_configuration_change(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 2.11 Calendar: a holiday list change between assignment and revision does not move
    the event's deadline; the event keeps its recorded calendar."""
    event_id = flag(client)
    monkeypatch.setenv("DEADLINE_CALENDAR", "working_days")
    monkeypatch.setenv("DEADLINE_HOLIDAYS", "2026-12-25,2026-12-26")
    fake_clock.set(at("2026-12-24T13:00:00Z"))
    first = ok(dispose(client, event_id, "OTHER_SERIOUS"))
    assert first["deadline_utc"] == "2027-01-15T13:00:00Z"

    monkeypatch.setenv("DEADLINE_CALENDAR", "calendar_days")
    monkeypatch.setenv("DEADLINE_HOLIDAYS", "")
    fake_clock.set(at("2026-12-27T09:00:00Z"))
    revised = ok(dispose(client, event_id, "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD"))
    # The recorded working-days calendar with its holidays: Mon 28 and Tue 29 December.
    assert revised["deadline_utc"] == "2026-12-29T13:00:00Z"
    calendars = {row["calendar"] for row in disposition_rows(owner_engine, event_id)}
    assert calendars == {"working_days;holidays=2026-12-25,2026-12-26"}


# Addendum rule 1 (decision D4): no silent extension, including on a reopen.

RULE_1 = {
    "reopen looser without rationale": (
        ["DEATH", "NOT_SERIOUS"],
        "OTHER_SERIOUS",
        None,
        409,
        None,
    ),
    "reopen looser with rationale": (
        ["DEATH", "NOT_SERIOUS"],
        "OTHER_SERIOUS",
        "the death was unrelated",
        200,
        15,
    ),
    "reopen to the same tier": (["DEATH", "NOT_SERIOUS"], "DEATH", None, 200, 10),
    "reopen stricter": (
        ["DEATH", "NOT_SERIOUS"],
        "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
        None,
        200,
        2,
    ),
    "looser than the most recent serious tier": (
        ["OTHER_SERIOUS", "DEATH"],
        "OTHER_SERIOUS",
        None,
        409,
        10,
    ),
    "no prior serious tier": (["NOT_SERIOUS"], "OTHER_SERIOUS", None, 200, 15),
}


@pytest.mark.parametrize(
    ("sequence", "tier", "rationale", "status", "days"), RULE_1.values(), ids=RULE_1.keys()
)
def test_addendum_rule_1(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    sequence: list[str],
    tier: str,
    rationale: str | None,
    status: int,
    days: int | None,
) -> None:
    """The six rows of Addendum rule 1's table. A NOT_SERIOUS step always carries a rationale."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    for step in sequence:
        ok(dispose(client, event_id, step, "not serious" if step == "NOT_SERIOUS" else None))
        fake_clock.advance(86_400)
    before = snapshot(owner_engine, event_id)
    response = dispose(client, event_id, tier, rationale)
    assert response.status_code == status
    if status == 409:
        assert response.json() == EXTENSION
        assert snapshot(owner_engine, event_id) == before
        expected_deadline = None if days is None else ANCHOR + timedelta(days=days)
        assert event_row(owner_engine, event_id)["deadline_utc"] == expected_deadline
        return
    assert days is not None
    result = response.json()
    assert result["state"] == "DISPOSITIONED"
    assert result["deadline_utc"] == (ANCHOR + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert result["disposition"]["rationale"] == rationale
    latest = disposition_rows(owner_engine, event_id)[-1]
    assert (latest["rationale"], latest["anchor_at"]) == (rationale, ANCHOR)
    entry = ledger_rows(owner_engine)[-1]
    assert entry["payload"]["rationale"] == rationale


# Decision D2: revisions into and out of NOT_SERIOUS (spec 2.6, 2.11; Addendum rule 11).


def test_d2_revisions_into_and_out_of_not_serious(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = assigned(fake_clock, client, "DEATH")

    # DEATH -> NOT_SERIOUS without a rationale: 400, nothing changes.
    fake_clock.set(ANCHOR + DAY)
    before = snapshot(owner_engine, event_id)
    response = dispose(client, event_id, "NOT_SERIOUS")
    assert (response.status_code, response.json()) == (400, RATIONALE_REQUIRED)
    assert snapshot(owner_engine, event_id) == before

    # With a rationale: CLOSED, deadline cleared, DISPOSITION_REVISED.
    closed = ok(dispose(client, event_id, "NOT_SERIOUS", "false alarm"))
    assert (closed["state"], closed["deadline_utc"]) == ("CLOSED", None)
    payload = ledger_rows(owner_engine)[-1]["payload"]
    assert ledger_rows(owner_engine)[-1]["entry_type"] == "DISPOSITION_REVISED"
    assert (payload["prior_tier"], payload["new_tier"]) == ("DEATH", "NOT_SERIOUS")
    assert (payload["prior_deadline_utc"], payload["deadline_utc"]) == (
        "2026-10-07T10:12:00Z",
        None,
    )

    # NOT_SERIOUS -> NOT_SERIOUS with a rationale: a revision, still CLOSED.
    fake_clock.set(ANCHOR + 2 * DAY)
    again = ok(dispose(client, event_id, "NOT_SERIOUS", "still a false alarm"))
    assert (again["state"], again["deadline_utc"]) == ("CLOSED", None)

    # Reopen to OTHER_SERIOUS 20 days later: looser than DEATH, so it needs a rationale
    # (Addendum rule 1); the deadline is from the first anchor even though it has passed.
    fake_clock.set(ANCHOR + 20 * DAY)
    response = dispose(client, event_id, "OTHER_SERIOUS")
    assert (response.status_code, response.json()) == (409, EXTENSION)
    reopened = ok(dispose(client, event_id, "OTHER_SERIOUS", "harm found after all"))
    assert reopened["state"] == "DISPOSITIONED"
    assert reopened["deadline_utc"] == "2026-10-12T10:12:00Z"
    assert parse(reopened["deadline_utc"]) < ANCHOR + 20 * DAY

    assert reopened["revisions"] == [
        {
            "prior_tier": "DEATH",
            "new_tier": "NOT_SERIOUS",
            "reviewer_id": "rev-1",
            "revised_at": "2026-09-28T10:12:00Z",
        },
        {
            "prior_tier": "NOT_SERIOUS",
            "new_tier": "NOT_SERIOUS",
            "reviewer_id": "rev-1",
            "revised_at": "2026-09-29T10:12:00Z",
        },
        {
            "prior_tier": "NOT_SERIOUS",
            "new_tier": "OTHER_SERIOUS",
            "reviewer_id": "rev-1",
            "revised_at": "2026-10-17T10:12:00Z",
        },
    ]
    assert [r["seq_in_event"] for r in disposition_rows(owner_engine, event_id)] == [1, 2, 3, 4]
    types = [e["entry_type"] for e in ledger_rows(owner_engine) if e["subject_id"] == event_id]
    assert types.count("DISPOSITION_RECORDED") == 1
    assert types.count("DISPOSITION_REVISED") == 3


# escalations.plan: called with the anchor and the new deadline, after the event update and
# before the ledger append (spec 2.4 steps 13 to 15, 9 Interfaces).


def test_plan_gets_the_anchor_and_the_deadline_between_the_event_update_and_the_append(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, datetime, datetime | None, str, int]] = []

    def recording_plan(
        session: Session, event_id: str, anchor: datetime, deadline_utc: datetime | None
    ) -> None:
        state = session.execute(
            text("SELECT state FROM event WHERE event_id = :e"), {"e": event_id}
        ).scalar_one()
        entries = session.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one()
        calls.append((event_id, anchor, deadline_utc, state, entries))

    monkeypatch.setattr(escalations, "plan", recording_plan)
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    ok(dispose(client, event_id, "OTHER_SERIOUS"))
    entries = head(owner_engine)
    fake_clock.set(ANCHOR + 3 * DAY)
    ok(dispose(client, event_id, "DEATH"))
    fake_clock.set(ANCHOR + 4 * DAY)
    ok(dispose(client, event_id, "NOT_SERIOUS", "not serious"))
    assert calls == [
        (event_id, ANCHOR, ANCHOR + 15 * DAY, "DISPOSITIONED", entries - 1),
        (event_id, ANCHOR, ANCHOR + 10 * DAY, "DISPOSITIONED", entries),
        (event_id, ANCHOR, None, "CLOSED", entries + 1),
    ]


# Atomicity (spec 2.11, 10): a failure at any write step leaves nothing behind.


def fail_after(real: Any) -> Any:
    def failing(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise RuntimeError("forced failure")

    return failing


@pytest.mark.parametrize("step", ["harm fields", "plan", "append"])
@pytest.mark.parametrize("revision", [False, True], ids=["first", "revision"])
def test_a_failure_at_any_step_rolls_everything_back(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    revision: bool,
) -> None:
    """Each forced failure happens after its step has written, so the rollback must undo the
    harm fields, the disposition row, the event update and any ledger entry."""
    event_id = flag(client)
    if revision:
        ok(dispose(client, event_id, "OTHER_SERIOUS"))
        fake_clock.advance(3600)
    before = snapshot(owner_engine, event_id)
    if step == "harm fields":
        monkeypatch.setattr(review, "apply_harm_fields", fail_after(review.apply_harm_fields))
    elif step == "plan":
        monkeypatch.setattr(escalations, "plan", fail_after(escalations.plan))
    else:
        monkeypatch.setattr(ledger, "append", fail_after(ledger.append))
    harm = {**decided("CONFIRMED"), RIGHTS: {"status": "CONFIRMED", "evidence": "changed"}}
    response = dispose(client, event_id, "DEATH", harm_fields=harm)
    assert response.status_code == 500
    assert snapshot(owner_engine, event_id) == before


# Concurrency (spec 2.11, 10): twenty parallel reviewer calls on one FLAGGED event.


def test_twenty_parallel_calls_give_one_recorded_and_nineteen_revised(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = flag(client)
    calls = 20

    async def fire() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as parallel:
            return await asyncio.gather(
                *(
                    parallel.post(
                        f"/v1/events/{event_id}/disposition",
                        headers=bearer("rev-1"),
                        json=body("OTHER_SERIOUS"),
                    )
                    for _ in range(calls)
                )
            )

    responses = asyncio.run(fire())
    assert [r.status_code for r in responses] == [200] * calls
    dispositions = disposition_rows(owner_engine, event_id)
    assert [row["seq_in_event"] for row in dispositions] == list(range(1, calls + 1))
    # No fork: each row supersedes the one before it.
    assert [row["supersedes_id"] for row in dispositions] == [
        None,
        *(row["disposition_id"] for row in dispositions[:-1]),
    ]
    types = [e["entry_type"] for e in ledger_rows(owner_engine) if e["subject_id"] == event_id]
    assert types.count("DISPOSITION_RECORDED") == 1
    assert types.count("DISPOSITION_REVISED") == calls - 1
    with Session(owner_engine) as session:
        assert verify_ledger(session)["valid"]
    assert len(detail(client, event_id)["revisions"]) == calls - 1


# dispositions.summary: which row feeds which field (spec 2.7).


def test_the_summary_takes_tier_and_rationale_from_the_latest_row_and_the_rest_from_the_first(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Rows written by two reviewers (only rev-1 is seeded, so the first row is inserted as the
    owner): disposition.reviewer_id and assigned_at are the first row's, tier and rationale the
    latest's; each revision names its own reviewer and time."""
    event_id = flag(client)
    first, second = "dsp-first", "dsp-second"
    insert = text(
        "INSERT INTO disposition (disposition_id, event_id, seq_in_event, tier, rationale,"
        " reviewer_id, assigned_at, anchor_at, calendar, deadline_utc, supersedes_id) VALUES"
        " (:id, :e, :seq, :tier, :rationale, :reviewer, :at, :anchor, 'calendar_days',"
        " :deadline, :supersedes)"
    )
    with owner_engine.begin() as conn:
        conn.execute(
            insert,
            {
                "id": first,
                "e": event_id,
                "seq": 1,
                "tier": "OTHER_SERIOUS",
                "rationale": "first reason",
                "reviewer": "rev-9",
                "at": ANCHOR,
                "anchor": ANCHOR,
                "deadline": ANCHOR + 15 * DAY,
                "supersedes": None,
            },
        )
        conn.execute(
            insert,
            {
                "id": second,
                "e": event_id,
                "seq": 2,
                "tier": "OTHER_SERIOUS",
                "rationale": None,
                "reviewer": "rev-1",
                "at": ANCHOR + DAY,
                "anchor": ANCHOR,
                "deadline": ANCHOR + 15 * DAY,
                "supersedes": first,
            },
        )
        conn.execute(
            text("UPDATE event SET state = 'DISPOSITIONED', deadline_utc = :d WHERE event_id = :e"),
            {"d": ANCHOR + 15 * DAY, "e": event_id},
        )
    shown = detail(client, event_id)
    assert shown["disposition"] == {
        "tier": "OTHER_SERIOUS",
        "rationale": None,
        "reviewer_id": "rev-9",
        "assigned_at": "2026-09-27T10:12:00Z",
    }
    assert shown["revisions"] == [
        {
            "prior_tier": "OTHER_SERIOUS",
            "new_tier": "OTHER_SERIOUS",
            "reviewer_id": "rev-1",
            "revised_at": "2026-09-28T10:12:00Z",
        }
    ]
    assert shown["deadline_utc"] == "2026-10-12T10:12:00Z"


def test_the_event_list_finds_dispositioned_and_closed_events(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = flag(client)
    ok(dispose(client, event_id, "DEATH"))
    listed = get(client, "/v1/events?state=DISPOSITIONED").json()
    assert [e["event_id"] for e in listed] == [event_id]
    ok(dispose(client, event_id, "NOT_SERIOUS", "fine"))
    listed = get(client, "/v1/events?state=CLOSED").json()
    assert [e["event_id"] for e in listed] == [event_id]


# Observability: one INFO line per recorded disposition, never the rationale or evidence text.


def test_one_log_line_per_disposition_without_the_rationale(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    event_id = flag(client)
    secret = "the patient in room 12 was harmed"
    harm = {**decided(), HEALTH: {"status": "CONFIRMED", "evidence": "evidence text 4711"}}
    ok(dispose(client, event_id, "NOT_SERIOUS", secret, harm_fields=harm))
    ok(dispose(client, event_id, "DEATH", secret))
    lines = [r for r in caplog.records if r.name == "api.disposition"]
    assert [(r.levelno, r.getMessage().split(":")[0]) for r in lines] == [
        (logging.INFO, "disposition recorded"),
        (logging.INFO, "disposition revised"),
    ]
    assert f"event_id={event_id}" in lines[0].getMessage()
    assert "tier=NOT_SERIOUS" in lines[0].getMessage()
    everything = caplog.text + repr([r.__dict__ for r in caplog.records])
    assert secret not in everything
    assert "evidence text 4711" not in everything
