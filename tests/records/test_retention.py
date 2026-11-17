"""AC-05 (REQ-05), Phase 5 spec 5.3: a flagged event and its evidence are retrievable at five
months and 29 days, and at six months and one day are either still present or gone (never a 500).
The clock is the fake clock, so the six-month jump runs no sweep (the HTTP version, in the
acceptance runner, does)."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from app import evidence
from app.formats import add_calendar_months, api_time, retain_until
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.records.conftest import post, rows
from tests.worker.conftest import FakeClock

T0_BREACH = "2026-09-27T10:05:00Z"


@pytest.fixture
def breach(client: ApiClient, fake_clock: FakeClock) -> str:
    """Four healthy samples, then the breach, all for sys-42's accuracy_drift at T0."""
    for minutes in (4, 3, 2, 1):
        stamp = api_time(fake_clock.now() - timedelta(minutes=minutes))
        assert post(client, value=0.1, timestamp=stamp).status_code == 201
    response = post(client, value=0.34, timestamp=T0_BREACH)
    assert response.status_code == 201, response.text
    event_id: str = response.json()["flagged_event_id"]
    return event_id


def _event(client: ApiClient, event_id: str) -> Any:
    return client.get(f"/v1/events/{event_id}", headers=bearer("viewer-1"))


def _stored(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    [row] = rows(
        owner_engine,
        "SELECT evidence_bundle, evidence_sha256, detected_at, retain_until FROM event"
        " WHERE event_id = :e",
        e=event_id,
    )
    return row


def test_ac05_retrievable_at_5mo29d(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine, breach: str
) -> None:
    at_flag = _event(client, breach)
    assert at_flag.status_code == 200
    detected = _stored(owner_engine, breach)["detected_at"]
    fake_clock.set(add_calendar_months(detected, 5) + timedelta(days=29))
    later = _event(client, breach)
    assert later.status_code == 200
    assert later.json() == at_flag.json()
    stored = _stored(owner_engine, breach)
    assert evidence.sha256_of(stored["evidence_bundle"]) == stored["evidence_sha256"]
    samples = rows(owner_engine, "SELECT sample_id FROM metric_sample")
    assert len(samples) == 5
    assert len(stored["evidence_bundle"]["samples"]) == 5


def test_ac05_at_6mo1d_presence_or_absence_is_accepted(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine, breach: str
) -> None:
    at_flag = _event(client, breach)
    stored = _stored(owner_engine, breach)
    detected = stored["detected_at"]
    # The rule of 5.1, read back from the stored row.
    assert stored["retain_until"] == max(
        add_calendar_months(detected, 6), detected + timedelta(days=183)
    )
    assert stored["retain_until"] == retain_until(detected)
    fake_clock.set(add_calendar_months(detected, 6) + timedelta(days=1))
    later = _event(client, breach)
    assert later.status_code in (200, 404)  # a minimum, not a maximum; anything else fails
    if later.status_code == 200:
        assert later.json() == at_flag.json()
