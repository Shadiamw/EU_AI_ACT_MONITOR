"""Phase 4 Part 1: the tests that wait for integration (Phase 4 Addendum rule 10, Part 1 rows).

Every test here is marked integration and needs Part 2's real POST /v1/events/{id}/disposition;
on a part branch they are skipped unless pytest runs with --run-integration. Time is the fake
clock of tests/part1/conftest.py throughout (it patches app.clock.now, which the disposition
handler reads for assigned_at, spec 2.4 steps 9 and 12); the HTTP test endpoints are never used.

Requests follow the contract's disposition request shape (contract, POST
/v1/events/{event_id}/disposition: {tier, rationale, harm_fields}) and use the reviewer key
rev-1. Bodies are sent as JSON text with every non-ASCII character escaped, so U+0000 and a lone
surrogate reach the server as the escapes \\u0000 and \\ud800.

Where Part 2 decides something the specification does not pin (the body of a successful
disposition), these tests assert only what the contract pins (200) and read the result back
through GET /v1/events/{id}, whose shape Part 1 owns.
"""

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from tests.api.conftest import MALFORMED, ApiClient
from tests.db_helpers import HARM_FIELDS, bearer
from tests.part1.conftest import T0, get, post, rows
from tests.part1.test_review_decisions import INCOMPLETE_INPUTS, MALFORMED_INPUTS, UNCONFIRMED
from tests.part1.test_review_prefill import AC07_AT, AC07_EVIDENCE, RIGHTS, post_ac07
from tests.worker.conftest import FakeClock

HEALTH, INFRASTRUCTURE, _, PROPERTY = HARM_FIELDS
# Contract, POST /v1/events/{event_id}/disposition request: the four tier strings and the two
# statuses a complete request may carry.
TIERS = ("DEATH", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS", "NOT_SERIOUS")
FINAL_STATUSES = ("CONFIRMED", "NOT_APPLICABLE")
ABSENT = object()


def disposition(
    tier: str, harm_fields: Any = ABSENT, rationale: str | None = None
) -> dict[str, Any]:
    """A request body in the contract's shape. The envelope is always checked; harm_fields is
    checked too unless the test sends a deliberately malformed or incomplete one."""
    assert tier in TIERS
    body: dict[str, Any] = {"tier": tier, "rationale": rationale}
    if harm_fields is not ABSENT:
        body["harm_fields"] = harm_fields
    return body


def complete(**fields: Any) -> dict[str, Any]:
    """Four harm-field decisions, in the contract's keys and statuses: rights CONFIRMED keeping
    its pre-fill and the other three NOT_APPLICABLE (spec 1.9, AC-07), with some changed."""
    harm = {
        HEALTH: {"status": "NOT_APPLICABLE", "evidence": None},
        INFRASTRUCTURE: {"status": "NOT_APPLICABLE", "evidence": None},
        RIGHTS: {"status": "CONFIRMED", "evidence": None},
        PROPERTY: {"status": "NOT_APPLICABLE", "evidence": None},
        **fields,
    }
    assert list(harm) == list(HARM_FIELDS)
    for value in harm.values():
        assert set(value) == {"status", "evidence"}
        assert value["status"] in FINAL_STATUSES
        assert value["evidence"] is None or isinstance(value["evidence"], str)
    return harm


def dispose(client: ApiClient, event_id: str, body: dict[str, Any]) -> httpx.Response:
    return client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers={**bearer("rev-1"), "Content-Type": "application/json"},
        content=json.dumps(body),
    )


def assert_recorded(response: httpx.Response) -> None:
    # The contract pins only "Success: 200, updated Event"; the body is read back through the
    # detail instead, so nothing here depends on how Part 2 renders it.
    assert response.status_code == 200, response.text


def parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def snapshot(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    ledger = rows(owner_engine, "SELECT * FROM ledger_entry ORDER BY seq DESC")
    return {
        "event": rows(owner_engine, "SELECT * FROM event WHERE event_id = :e", e=event_id),
        "harm_fields": rows(
            owner_engine,
            "SELECT * FROM harm_field WHERE event_id = :e ORDER BY field_key",
            e=event_id,
        ),
        "ledger_count": len(ledger),
        "ledger_last": ledger[0],
    }


REJECTED: dict[str, tuple[dict[str, Any], int, dict[str, str]]] = {
    **{
        f"400 {name}": (disposition("OTHER_SERIOUS", raw), 400, MALFORMED)
        for name, raw in MALFORMED_INPUTS.items()
    },
    "409 harm_fields absent": (disposition("OTHER_SERIOUS"), 409, UNCONFIRMED),
    **{
        f"409 {name}": (disposition("OTHER_SERIOUS", raw), 409, UNCONFIRMED)
        for name, (raw, _) in INCOMPLETE_INPUTS.items()
    },
}


@pytest.mark.integration
@pytest.mark.parametrize(("body", "status", "error"), REJECTED.values(), ids=REJECTED.keys())
def test_rejected_harm_fields_write_nothing_and_append_nothing(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    body: dict[str, Any],
    status: int,
    error: dict[str, str],
) -> None:
    """Spec 1.5 and 1.9 (validation matrix): every 400 and 409 input of spec 1.5, sent to the
    disposition endpoint, returns its exact body and leaves the event row, the four harm fields
    and the ledger as they were. Needs Part 2's disposition endpoint. Clock: FakeClock."""
    event_id = post_ac07(client, fake_clock)
    before = snapshot(owner_engine, event_id)
    response = dispose(client, event_id, body)
    assert (response.status_code, response.json()) == (status, error)
    assert snapshot(owner_engine, event_id) == before


@pytest.mark.integration
def test_ac07_confirmation_record_through_the_api(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 1.9 AC-07 confirmation record: after a disposition with rights CONFIRMED and three
    NOT_APPLICABLE, each field shows its status, confirmed_by rev-1 and confirmed_at equal to
    disposition.assigned_at. Needs Part 2's disposition endpoint and dispositions.summary.
    Clock: FakeClock, still at the AC-07 sample time when the disposition is made."""
    event_id = post_ac07(client, fake_clock)
    assert_recorded(dispose(client, event_id, disposition("OTHER_SERIOUS", complete())))
    detail = get(client, f"/v1/events/{event_id}").json()
    assigned_at = detail["disposition"]["assigned_at"]
    # Spec 2.4 steps 9 and 12: assigned_at is the clock's now.
    assert parse(assigned_at) == AC07_AT
    assert detail["harm_fields"] == {
        key: {
            "status": "CONFIRMED" if key == RIGHTS else "NOT_APPLICABLE",
            "evidence": AC07_EVIDENCE if key == RIGHTS else None,
            "confirmed_by": "rev-1",
            "confirmed_at": assigned_at,
        }
        for key in HARM_FIELDS
    }


@pytest.mark.integration
def test_an_evidence_override_through_the_api_keeps_the_prefill(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 1.9 evidence override: CONFIRMED with new evidence stores it; CONFIRMED with null
    keeps the pre-fill; prefill_evidence in the database is unchanged both times. Needs Part
    2's disposition endpoint (the second call is a same-tier revision, spec 2.3). Clock:
    FakeClock, advanced one hour between the two calls."""
    event_id = post_ac07(client, fake_clock)

    def stored_prefill() -> Any:
        [row] = rows(
            owner_engine,
            "SELECT prefill_evidence FROM harm_field WHERE event_id = :e AND field_key = :k",
            e=event_id,
            k=RIGHTS,
        )
        return row["prefill_evidence"]

    override = "reviewer evidence: the drift reached decisions about natural persons"
    rights = {"status": "CONFIRMED", "evidence": override}
    assert_recorded(
        dispose(client, event_id, disposition("OTHER_SERIOUS", complete(**{RIGHTS: rights})))
    )
    assert get(client, f"/v1/events/{event_id}").json()["harm_fields"][RIGHTS]["evidence"] == (
        override
    )
    assert stored_prefill() == AC07_EVIDENCE

    fake_clock.advance(3600)
    assert_recorded(dispose(client, event_id, disposition("OTHER_SERIOUS", complete())))
    assert get(client, f"/v1/events/{event_id}").json()["harm_fields"][RIGHTS]["evidence"] == (
        AC07_EVIDENCE
    )
    assert stored_prefill() == AC07_EVIDENCE


@pytest.mark.integration
def test_a_revision_through_the_api_touches_only_the_changed_field(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 1.9 revision touches only changed fields: a revision that changes one field's status
    updates only that field's confirmer and time. Needs Part 2's disposition endpoint (a
    same-tier revision, spec 2.3). Clock: FakeClock, advanced one hour before the revision."""
    event_id = post_ac07(client, fake_clock)
    assert_recorded(dispose(client, event_id, disposition("OTHER_SERIOUS", complete())))
    first = get(client, f"/v1/events/{event_id}").json()["harm_fields"]

    fake_clock.advance(3600)
    revised_at = AC07_AT + timedelta(hours=1)
    health = {"status": "CONFIRMED", "evidence": "a patient was harmed by a routing decision"}
    assert_recorded(
        dispose(client, event_id, disposition("OTHER_SERIOUS", complete(**{HEALTH: health})))
    )
    after = get(client, f"/v1/events/{event_id}").json()["harm_fields"]

    assert after[HEALTH] == {
        **health,
        "confirmed_by": "rev-1",
        "confirmed_at": "2026-10-05T10:00:00Z",
    }
    assert parse(after[HEALTH]["confirmed_at"]) == revised_at
    for key in (INFRASTRUCTURE, RIGHTS, PROPERTY):
        assert after[key] == first[key]
        assert after[key]["confirmed_at"] == "2026-10-05T09:00:00Z"


@pytest.mark.integration
def test_ac17_detection_time_and_assignment_anchored_deadline(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 1.9 AC-17 (REQ-15): flag at T, advance 14,400 s, assign DEATH. The detail shows
    detected_at = T, disposition.assigned_at within 2 s of T + 4 h, deadline_utc = assigned_at
    + 10 days, and assigned_at - detected_at >= 14,400 s; a later revision leaves detected_at
    and assigned_at unchanged. Needs Part 2's disposition endpoint and its real deadlines
    (spec 2.5, DEATH 10 days, the default calendar_days calendar). Clock: FakeClock; T is
    contract Example 1's 2026-09-27T10:05:00Z."""
    response = post(client)  # contract Example 1, sampled and flagged at T
    assert response.status_code == 201
    event_id = response.json()["flagged_event_id"]

    fake_clock.advance(14_400)
    assert_recorded(dispose(client, event_id, disposition("DEATH", complete())))
    detail = get(client, f"/v1/events/{event_id}").json()

    detected_at = parse(detail["detected_at"])
    assigned_at = parse(detail["disposition"]["assigned_at"])
    assert detail["detected_at"] == "2026-09-27T10:05:00Z"
    assert detected_at == T0
    assert abs((assigned_at - (T0 + timedelta(hours=4))).total_seconds()) <= 2
    assert parse(detail["deadline_utc"]) == assigned_at + timedelta(days=10)
    assert (assigned_at - detected_at).total_seconds() >= 14_400

    # A later revision, to the stricter CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD (no rationale
    # needed, spec 2.5), moves neither timestamp.
    fake_clock.advance(86_400)
    assert_recorded(
        dispose(client, event_id, disposition("CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", complete()))
    )
    revised = get(client, f"/v1/events/{event_id}").json()
    assert revised["detected_at"] == detail["detected_at"]
    assert revised["disposition"]["assigned_at"] == detail["disposition"]["assigned_at"]
