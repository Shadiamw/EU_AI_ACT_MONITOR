"""Phase 4 Part 2: the tests that wait for integration (Phase 4 Addendum rule 10, Part 2 row).

Every test here is marked integration and needs Part 3's escalation table (migration 0007) and
its real escalations.plan; on a part branch they are skipped unless pytest runs with
--run-integration. They check the escalation rows only, never a delivered reminder, so no
webhook and no fire_due pass is involved. Time is the fake clock (FakeClock) throughout; the
HTTP test endpoints are never used.

The expected rows come from spec 3.3 and Addendum rule 5: the default ESCALATION_PERCENTAGES
(50, 80, 100), due_at = escalations.instant(anchor, deadline, pct), a NOT_SERIOUS revision
cancels PENDING rows, and a later serious plan puts CANCELLED rows back to PENDING with the
recomputed due_at.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from app.escalations import instant
from tests.api.conftest import ApiClient
from tests.part1.conftest import rows
from tests.part2.conftest import ANCHOR, EXTENSION, dispose, flag, snapshot
from tests.worker.conftest import FakeClock

DAY = timedelta(days=1)
PERCENTAGES = (50, 80, 100)


def escalations(owner_engine: Engine, event_id: str) -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT pct, status, due_at FROM escalation WHERE event_id = :e ORDER BY pct",
        e=event_id,
    )


def planned(status: str, deadline: datetime) -> list[dict[str, Any]]:
    return [
        {"pct": pct, "status": status, "due_at": instant(ANCHOR, deadline, pct)}
        for pct in PERCENTAGES
    ]


def ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.mark.integration
def test_ac09_a_not_serious_first_disposition_has_no_escalation_rows(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 2.11 AC-09 rationale path: with a rationale, 200, CLOSED, deadline_utc null and no
    escalation rows (Model 5 Ex-2). Needs Part 3's escalation table and plan. Clock:
    FakeClock."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    closed = ok(dispose(client, event_id, "NOT_SERIOUS", "seasonal shift, no user impact"))
    assert (closed["state"], closed["deadline_utc"]) == ("CLOSED", None)
    assert escalations(owner_engine, event_id) == []


@pytest.mark.integration
def test_d2_revisions_cancel_and_replan_the_reminders(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 2.11 D2 revisions: DEATH plans three PENDING rows; NOT_SERIOUS cancels them;
    NOT_SERIOUS again leaves them cancelled; the reopen to OTHER_SERIOUS (with the rationale
    Addendum rule 1 requires) puts them back to PENDING at the 15-day instants from the first
    anchor, even though those instants have passed. Needs Part 3's escalation table and plan.
    Clock: FakeClock."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    ok(dispose(client, event_id, "DEATH"))
    assert escalations(owner_engine, event_id) == planned("PENDING", ANCHOR + 10 * DAY)

    fake_clock.set(ANCHOR + DAY)
    ok(dispose(client, event_id, "NOT_SERIOUS", "false alarm"))
    assert escalations(owner_engine, event_id) == planned("CANCELLED", ANCHOR + 10 * DAY)

    fake_clock.set(ANCHOR + 2 * DAY)
    ok(dispose(client, event_id, "NOT_SERIOUS", "still a false alarm"))
    assert [row["status"] for row in escalations(owner_engine, event_id)] == ["CANCELLED"] * 3

    fake_clock.set(ANCHOR + 20 * DAY)
    reopened = ok(dispose(client, event_id, "OTHER_SERIOUS", "harm found after all"))
    assert reopened["deadline_utc"] == "2026-10-12T10:12:00Z"
    assert escalations(owner_engine, event_id) == planned("PENDING", ANCHOR + 15 * DAY)


@pytest.mark.integration
def test_a_stricter_revision_moves_the_unsent_reminders(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 2.4 step 14 with spec 3.3: OTHER_SERIOUS then DEATH three days later re-plans the
    PENDING rows for the 10-day window from the same anchor (Model 5 Ex-1). Needs Part 3's
    escalation table and plan. Clock: FakeClock."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    ok(dispose(client, event_id, "OTHER_SERIOUS"))
    assert escalations(owner_engine, event_id) == planned("PENDING", ANCHOR + 15 * DAY)
    fake_clock.set(ANCHOR + 3 * DAY)
    ok(dispose(client, event_id, "DEATH"))
    assert escalations(owner_engine, event_id) == planned("PENDING", ANCHOR + 10 * DAY)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("tier", "rationale", "status", "days"),
    [
        ("OTHER_SERIOUS", None, 409, None),
        ("OTHER_SERIOUS", "the death was unrelated", 200, 15),
        ("DEATH", None, 200, 10),
        ("CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", None, 200, 2),
    ],
    ids=["looser-no-rationale", "looser-with-rationale", "same", "stricter"],
)
def test_addendum_rule_1_reopens_and_the_reminders(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    tier: str,
    rationale: str | None,
    status: int,
    days: int | None,
) -> None:
    """Addendum rule 1, the reopen rows of its table, with the reminders: DEATH, then
    NOT_SERIOUS (rows CANCELLED), then the reopen. A 409 writes nothing, so the rows stay
    CANCELLED; a success re-plans them for the new window from the first anchor. Needs Part 3's
    escalation table and plan. Clock: FakeClock."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    ok(dispose(client, event_id, "DEATH"))
    fake_clock.set(ANCHOR + DAY)
    ok(dispose(client, event_id, "NOT_SERIOUS", "false alarm"))
    before = snapshot(owner_engine, event_id)
    cancelled = escalations(owner_engine, event_id)

    fake_clock.set(ANCHOR + 2 * DAY)
    response = dispose(client, event_id, tier, rationale)
    assert response.status_code == status
    if days is None:
        assert response.json() == EXTENSION
        assert snapshot(owner_engine, event_id) == before
        assert escalations(owner_engine, event_id) == cancelled
    else:
        assert escalations(owner_engine, event_id) == planned("PENDING", ANCHOR + days * DAY)


@pytest.mark.integration
def test_a_looser_revision_without_a_rationale_leaves_the_reminders_alone(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Addendum rule 1, OTHER_SERIOUS, DEATH, OTHER_SERIOUS without a rationale: 409, and the
    rows keep the DEATH instants. Needs Part 3's escalation table and plan. Clock: FakeClock."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    ok(dispose(client, event_id, "OTHER_SERIOUS"))
    fake_clock.set(ANCHOR + DAY)
    ok(dispose(client, event_id, "DEATH"))
    fake_clock.set(ANCHOR + 2 * DAY)
    response = dispose(client, event_id, "OTHER_SERIOUS")
    assert (response.status_code, response.json()) == (409, EXTENSION)
    assert escalations(owner_engine, event_id) == planned("PENDING", ANCHOR + 10 * DAY)
