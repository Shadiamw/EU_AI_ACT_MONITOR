"""Phase 4 Part 3: the tests that wait for integration (Addendum rule 10, Part 3 row).

Every test here is marked integration and needs Part 2's real disposition endpoint and Part 4's
clock-advance endpoint, which requires a key and runs the sweep, fire_due and delivery once each
(spec 4.5). Clock: the database offset moved through that endpoint, never FakeClock, because
FakeClock patches only the test process (Addendum rule 10). Sample timestamps use the simulated
now (Addendum rule 7), and every count filters by notification_type.
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import platform, worker
from app.escalations import instant
from app.main import create_app
from app.platform import with_phase4_tables
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import OTHER_TABLES
from tests.part2.conftest import dispose, parse
from tests.part3.conftest import DAY, reminders, triggered
from tests.worker.conftest import Receiver
from tests.worker.conftest import receiver as receiver
from tools.seed_part1 import seed

pytestmark = pytest.mark.integration


@pytest.fixture
def live(owner_engine: Engine) -> Iterator[ApiClient]:
    """The real app on the real clock plus the database offset, from the seed."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, OTHER_TABLES))}"))
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    platform.set_offset_seconds(0)
    with ApiClient(create_app()) as client:
        yield client
    platform.set_offset_seconds(0)


def advance(client: ApiClient, seconds: int = 0) -> datetime:
    response = client.request(
        "POST", "/v1/test/clock/advance", headers=bearer("rev-1"), json={"seconds": seconds}
    )
    assert response.status_code == 200, response.text
    return parse(response.json()["simulated_time"])


def advance_to(client: ApiClient, moment: datetime) -> None:
    """To the first whole second at or after `moment`."""
    gap = moment - advance(client)
    if gap > timedelta(0):
        advance(client, -(-gap // timedelta(seconds=1)))


def flag(client: ApiClient) -> str:
    """A new breach at simulated now, on accuracy (below 0.70 breaches thr-2)."""
    at = advance(client).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    response = client.request(
        "POST",
        "/v1/metrics",
        headers=bearer("sys-42"),
        json={
            "system_id": "sys-42",
            "metric_name": "accuracy",
            "metric_type": "performance",
            "value": 0.61,
            "timestamp": at,
        },
    )
    assert response.status_code == 201, response.text
    event_id: str = response.json()["flagged_event_id"]
    return event_id


def register(client: ApiClient, receiver: Receiver) -> None:
    response = client.request(
        "POST",
        "/v1/webhooks",
        headers=bearer("rev-1"),
        json={"url": receiver.url, "event_types": ["reminder"]},
    )
    assert response.status_code == 201, response.text


def decided(client: ApiClient, event_id: str, tier: str, rationale: str | None = None) -> Any:
    response = dispose(client, event_id, tier, rationale)
    assert response.status_code == 200, response.text
    return response.json()


def received(receiver: Receiver) -> list[int]:
    return [body["escalation_pct"] for body in reminders(receiver.calls)]


def race(app_engine: Engine, client: ApiClient, seconds: int) -> None:
    """A worker pass and a clock advance leave a barrier together. An exception in the worker
    pass fails the test."""
    barrier = threading.Barrier(2)

    def worker_pass() -> None:
        barrier.wait()
        worker.run_iteration(app_engine, sweep=False)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker_pass)
        barrier.wait()
        advance(client, seconds)
        future.result()


def test_model_5_ex_1_a_stricter_revision_through_the_api(
    live: ApiClient, receiver: Receiver
) -> None:
    """OTHER_SERIOUS, DEATH on day 3: reminders at +5d, +8d, +10d of the first assignment, and
    none at +7d 12:00."""
    register(live, receiver)
    event_id = flag(live)
    first = decided(live, event_id, "OTHER_SERIOUS")
    anchor = parse(first["disposition"]["assigned_at"])
    advance_to(live, anchor + 3 * DAY)
    revised = decided(live, event_id, "DEATH")
    assert parse(revised["deadline_utc"]) == anchor + 10 * DAY
    assert parse(revised["disposition"]["assigned_at"]) == anchor

    advance_to(live, anchor + 5 * DAY - timedelta(seconds=30))
    assert received(receiver) == []
    advance_to(live, anchor + 5 * DAY)
    assert received(receiver) == [50]
    advance_to(live, anchor + 7 * DAY + timedelta(hours=12))
    assert received(receiver) == [50]
    advance_to(live, anchor + 8 * DAY)
    assert received(receiver) == [50, 80]
    advance_to(live, anchor + 10 * DAY)
    assert received(receiver) == [50, 80, 100]
    assert [body["tier"] for body in reminders(receiver.calls)] == ["DEATH"] * 3


def test_a_looser_revision_with_a_rationale_through_the_api(
    live: ApiClient, receiver: Receiver
) -> None:
    register(live, receiver)
    event_id = flag(live)
    first = decided(live, event_id, "DEATH")
    anchor = parse(first["disposition"]["assigned_at"])
    decided(live, event_id, "OTHER_SERIOUS", "the death was unrelated")
    advance_to(live, anchor + 6 * DAY)
    assert received(receiver) == []
    deadline = anchor + 15 * DAY
    advance_to(live, instant(anchor, deadline, 50))
    assert received(receiver) == [50]


def test_not_serious_then_reopen_through_the_api(
    live: ApiClient, receiver: Receiver, owner_engine: Engine
) -> None:
    """D2 and Addendum rule 1: 50% sent; NOT_SERIOUS cancels the rest and none fires while
    closed; the reopen re-plans them; 50% is not resent."""
    register(live, receiver)
    event_id = flag(live)
    first = decided(live, event_id, "DEATH")
    anchor = parse(first["disposition"]["assigned_at"])
    advance_to(live, anchor + 5 * DAY)
    assert received(receiver) == [50]
    decided(live, event_id, "NOT_SERIOUS", "false alarm")
    advance_to(live, anchor + 11 * DAY)
    assert received(receiver) == [50]
    decided(live, event_id, "DEATH")
    advance(live)  # the instants have passed: late, never early
    assert received(receiver) == [50, 80, 100]
    assert len(triggered(owner_engine)) == 3


def test_no_duplicates_through_the_clock_advance_endpoint(
    live: ApiClient, receiver: Receiver, owner_engine: Engine, app_engine: Engine
) -> None:
    """Spec 3.9: ten further advances and worker passes after 100%: still three."""
    register(live, receiver)
    event_id = flag(live)
    first = decided(live, event_id, "OTHER_SERIOUS")
    anchor = parse(first["disposition"]["assigned_at"])
    advance_to(live, anchor + 20 * DAY)
    assert received(receiver) == [50, 80, 100]
    for _ in range(10):
        advance(live, 3600)
        worker.run_iteration(app_engine, sweep=False)
    assert received(receiver) == [50, 80, 100]
    assert len(triggered(owner_engine)) == 3


def test_a_worker_pass_and_a_clock_advance_across_the_50_instant_50_times(
    live: ApiClient, owner_engine: Engine, app_engine: Engine
) -> None:
    """Spec 3.9 Concurrency and Integration Guide 5: one reminder each time."""
    for _ in range(50):
        platform.reset()
        event_id = flag(live)
        first = decided(live, event_id, "OTHER_SERIOUS")
        anchor = parse(first["disposition"]["assigned_at"])
        gap = instant(anchor, anchor + 15 * DAY, 50) - advance(live)
        race(app_engine, live, -(-gap // timedelta(seconds=1)))
        worker.run_iteration(app_engine, sweep=False)
        entries = triggered(owner_engine)
        assert [entry["payload"]["escalation_pct"] for entry in entries] == [50]


def test_healthz_shows_a_real_overdue_row(live: ApiClient, owner_engine: Engine) -> None:
    """Spec 3.6 and 4.5. A clock advance fires what it crosses, so the row is pushed into the
    past directly, as a stopped worker would leave it."""
    event_id = flag(live)
    decided(live, event_id, "DEATH")
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE escalation SET due_at = due_at - interval '6 days' WHERE pct = 50")
        )
    response = live.get("/healthz")
    assert response.json()["overdue_escalations"] == 1
