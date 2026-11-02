"""Fixtures and helpers for the Phase 4 Part 2 tests: the real app, real keys and a real
PostgreSQL database, as for Part 1 (tests/part1/conftest.py, whose fixtures are re-exported
here).

Time is the fake clock throughout (it patches app.clock.now, which the disposition handler reads
for assigned_at, spec 2.4 steps 9 and 12); the HTTP test endpoints are never used (Addendum rule
10). Requests carry a real `Authorization: Bearer <key>` header.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import Engine, text

from tests.api.conftest import ApiClient
from tests.db_helpers import HARM_FIELDS, bearer, insert_event
from tests.part1.conftest import client as client
from tests.part1.conftest import fake_clock as fake_clock
from tests.part1.conftest import post, rows
from tests.part1.conftest import seeded as seeded

# The anchor of the spec's worked examples (spec 2.5; contract Example 2's assigned_at).
ANCHOR = datetime(2026, 9, 27, 10, 12, tzinfo=UTC)

SERIOUS = ("CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "DEATH", "OTHER_SERIOUS")
TIERS = ("DEATH", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS", "NOT_SERIOUS")

RATIONALE_REQUIRED = {
    "error_code": "RATIONALE_REQUIRED",
    "message": "rationale is required when tier is NOT_SERIOUS",
}
EXTENSION = {
    "error_code": "DEADLINE_EXTENSION_REQUIRES_RATIONALE",
    "message": "extending a running deadline requires a rationale",
}
NOT_REVIEWABLE = {
    "error_code": "EVENT_NOT_REVIEWABLE",
    "message": "a monitoring gap event cannot be dispositioned",
}
NO_DISPOSITION = {
    "error_code": "NO_DISPOSITION",
    "message": "no disposition has been recorded for this event",
}
UNCONFIRMED = {
    "error_code": "HARM_FIELDS_UNCONFIRMED",
    "message": "all four harm fields must be CONFIRMED or NOT_APPLICABLE",
}
NOT_IMPLEMENTED = {"error_code": "NOT_IMPLEMENTED", "message": "not implemented in this phase"}


def decided(status: str = "NOT_APPLICABLE") -> dict[str, dict[str, Any]]:
    """Four complete harm-field decisions, all with the same status and null evidence."""
    return {key: {"status": status, "evidence": None} for key in HARM_FIELDS}


def body(tier: str, rationale: Any = None, harm_fields: Any = None) -> dict[str, Any]:
    """A request in the contract's shape, {tier, rationale, harm_fields}; harm_fields defaults
    to four NOT_APPLICABLE decisions."""
    return {
        "tier": tier,
        "rationale": rationale,
        "harm_fields": decided() if harm_fields is None else harm_fields,
    }


def dispose(
    client: ApiClient,
    event_id: str,
    tier: str,
    rationale: Any = None,
    *,
    actor: str | None = "rev-1",
    harm_fields: Any = None,
) -> httpx.Response:
    headers = {} if actor is None else bearer(actor)
    return client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers=headers,
        json=body(tier, rationale, harm_fields),
    )


def report(client: ApiClient, event_id: str, actor: str | None = "rev-1") -> httpx.Response:
    headers = {} if actor is None else bearer(actor)
    return client.get(f"/v1/events/{event_id}/report", headers=headers)


def flag(client: ApiClient) -> str:
    """Contract Example 1 on sys-42 (a breach of thr-1): a new FLAGGED event."""
    response = post(client)
    assert response.status_code == 201, response.text
    event_id: str = response.json()["flagged_event_id"]
    return event_id


def add_gap_event(owner_engine: Engine, event_id: str = "evt-gap") -> str:
    """A monitoring-gap event as Part 3's sweeper writes it (four NOT_APPLICABLE fields)."""
    with owner_engine.begin() as conn:
        insert_event(conn, event_id, event_type="monitoring_gap", detected_at=ANCHOR)
    return event_id


def parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def disposition_rows(owner_engine: Engine, event_id: str) -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT * FROM disposition WHERE event_id = :e ORDER BY seq_in_event",
        e=event_id,
    )


def ledger_rows(owner_engine: Engine) -> list[dict[str, Any]]:
    return rows(owner_engine, "SELECT * FROM ledger_entry ORDER BY seq")


def event_row(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    [row] = rows(owner_engine, "SELECT * FROM event WHERE event_id = :e", e=event_id)
    return row


def snapshot(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    """Everything a disposition call can write: the event row, its harm fields, its disposition
    rows and the whole ledger."""
    return {
        "event": event_row(owner_engine, event_id),
        "harm_fields": rows(
            owner_engine,
            "SELECT * FROM harm_field WHERE event_id = :e ORDER BY field_key",
            e=event_id,
        ),
        "dispositions": disposition_rows(owner_engine, event_id),
        "ledger": ledger_rows(owner_engine),
    }


def set_state(owner_engine: Engine, event_id: str, state: str) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE event SET state = :s WHERE event_id = :e"), {"s": state, "e": event_id}
        )
