"""A breach after the event is CLOSED starts a new incident (decision 227; Phase 4 spec 2).

Phase 3's spec 1.4 steps 9 to 11 join a breach to the open breach_series of its (system,
metric) and only a healthy sample ends the series; Phase 3 events never left FLAGGED. In Phase 4
a NOT_SERIOUS disposition closes the event, and spec 2 says a breach "creates a FLAGGED event".
So while the series' event is FLAGGED, UNDER_REVIEW or DISPOSITIONED a breach still joins it,
and when it is CLOSED the breach creates a new event, its EVENT_FLAGGED entry, its flag
notification and a new pre-fill.

Integration-marked: they need Part 2's real POST /v1/events/{id}/disposition. Clock: the fake
clock of tests/part1/conftest.py, starting at contract Example 1's timestamp.
"""

from typing import Any

import pytest
from sqlalchemy import Engine

from app import gaps
from tests.api.conftest import ApiClient
from tests.part1.conftest import get, post, rows
from tests.part1.test_review_integration import complete, dispose, disposition
from tests.worker.conftest import FakeClock

pytestmark = pytest.mark.integration

NOT_SERIOUS = disposition("NOT_SERIOUS", complete(), rationale="seasonal shift, no user impact")


def breach(client: ApiClient, fake_clock: FakeClock, **changes: Any) -> str:
    """A breaching accuracy_drift sample (0.34 against thr-1's 0.30) at the clock's time."""
    fake_clock.advance(60)
    stamp = fake_clock.now().isoformat().replace("+00:00", "Z")
    response = post(client, timestamp=stamp, **changes)
    assert response.status_code == 201, response.text
    event_id: str = response.json()["flagged_event_id"]
    return event_id


def counts(owner_engine: Engine) -> dict[str, int]:
    def one(sql: str) -> int:
        [row] = rows(owner_engine, sql)
        return int(row["n"])

    return {
        "events": one("SELECT count(*) AS n FROM event"),
        "ledger": one("SELECT count(*) AS n FROM ledger_entry"),
        "notifications": one("SELECT count(*) AS n FROM notification"),
        "harm_fields": one("SELECT count(*) AS n FROM harm_field"),
    }


def state(owner_engine: Engine, event_id: str) -> str:
    [row] = rows(owner_engine, "SELECT state FROM event WHERE event_id = :e", e=event_id)
    return str(row["state"])


def test_a_breach_after_a_not_serious_closure_creates_a_new_flagged_event(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    first = breach(client, fake_clock)
    assert dispose(client, first, NOT_SERIOUS).status_code == 200
    assert state(owner_engine, first) == "CLOSED"
    before = counts(owner_engine)

    second = breach(client, fake_clock)

    assert second != first
    assert state(owner_engine, second) == "FLAGGED"
    assert state(owner_engine, first) == "CLOSED"
    assert counts(owner_engine) == {
        "events": before["events"] + 1,
        "ledger": before["ledger"] + 2,
        "notifications": before["notifications"] + 1,
        "harm_fields": before["harm_fields"] + 4,
    }
    new_entries = rows(
        owner_engine,
        "SELECT entry_type, subject_id FROM ledger_entry WHERE seq > :n ORDER BY seq",
        n=before["ledger"],
    )
    assert [(e["entry_type"], e["subject_id"]) for e in new_entries] == [
        ("EVENT_FLAGGED", second),
        ("NOTIFICATION_REQUESTED", second),
    ]
    [notification] = rows(
        owner_engine,
        "SELECT notification_type FROM notification WHERE event_id = :e",
        e=second,
    )
    assert notification["notification_type"] == "flag_notification"
    statuses = rows(owner_engine, "SELECT status FROM harm_field WHERE event_id = :e", e=second)
    assert [s["status"] for s in statuses] == ["PREFILLED_PENDING_CONFIRMATION"] * 4
    series = rows(
        owner_engine,
        "SELECT event_id, closed_at IS NULL AS open, occurrence_count FROM breach_series"
        " ORDER BY opened_at",
    )
    assert [(s["event_id"], s["open"], s["occurrence_count"]) for s in series] == [
        (first, False, 1),
        (second, True, 1),
    ]

    # The new incident is open, so the next breach joins it.
    assert breach(client, fake_clock) == second


@pytest.mark.parametrize("held_state", ["FLAGGED", "UNDER_REVIEW", "DISPOSITIONED"])
def test_a_breach_while_the_event_is_open_returns_it_and_appends_nothing(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, held_state: str
) -> None:
    first = breach(client, fake_clock)
    if held_state != "FLAGGED":
        assert get(client, f"/v1/events/{first}", actor="rev-1").status_code == 200
    if held_state == "DISPOSITIONED":
        assert dispose(client, first, disposition("OTHER_SERIOUS", complete())).status_code == 200
    assert state(owner_engine, first) == held_state
    before = counts(owner_engine)

    assert breach(client, fake_clock) == first

    assert counts(owner_engine) == before
    assert state(owner_engine, first) == held_state


def test_a_gap_event_is_unaffected(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """A gap opens and the next (breaching) sample closes it as before; the CLOSED gap event
    is not a breach series, so the breach gets its own event and the next breach joins it."""
    fake_clock.advance(900)
    opened = gaps.sweep(owner_engine)
    assert len(opened) == 1
    [gap] = opened

    first = breach(client, fake_clock)

    assert first != gap
    assert state(owner_engine, gap) == "CLOSED"
    assert breach(client, fake_clock) == first
    [gap_row] = rows(owner_engine, "SELECT type FROM event WHERE event_id = :e", e=gap)
    assert gap_row["type"] == "monitoring_gap"


def test_the_other_metric_still_gets_its_own_event(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    accuracy = {"metric_name": "accuracy", "metric_type": "performance", "value": 0.61}
    drift = breach(client, fake_clock)
    performance = breach(client, fake_clock, **accuracy)
    assert performance != drift

    assert dispose(client, drift, NOT_SERIOUS).status_code == 200
    assert breach(client, fake_clock, **accuracy) == performance
    new_drift = breach(client, fake_clock)
    assert new_drift not in (drift, performance)
