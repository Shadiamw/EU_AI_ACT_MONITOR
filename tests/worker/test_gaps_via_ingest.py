"""Part 3's gap rules through Part 1's real ingest (spec 3.4 and 3.9).

Two rows of spec 3.9 were deferred until Part 1 existed, because they are about what the ingest
does to last_received_at: "Duplicate resets silence" and "Wrong sender clock". Recovery is
repeated here through POST /v1/metrics as well; tests/worker/test_gaps.py keeps its in-process
version of the ingest step, which tests gaps.close_if_open on its own.

Times are on the fake clock, which starts at T0 = 2026-10-01T09:00:00Z. The seed sets
last_received_at to T0; the gap timeout is 3 x 300 = 900 s.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import gaps
from app.main import create_app
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.worker.conftest import T0, FakeClock, entry_types, rows
from tools.seed_part1 import seed


@pytest.fixture
def client(owner_engine: Engine, fake_clock: FakeClock) -> Iterator[ApiClient]:
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    with ApiClient(create_app()) as api_client:
        yield api_client


def send(client: ApiClient, timestamp: str, value: Any = 0.10) -> httpx.Response:
    return client.request(
        "POST",
        "/v1/metrics",
        headers=bearer("sys-42"),
        json={
            "system_id": "sys-42",
            "metric_name": "accuracy_drift",
            "metric_type": "drift",
            "value": value,
            "timestamp": timestamp,
        },
    )


def last_received_at(owner_engine: Engine) -> Any:
    [row] = rows(owner_engine, "SELECT last_received_at FROM monitored_system")
    return row["last_received_at"]


def test_duplicate_resets_silence(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    assert send(client, "2026-10-01T09:00:00Z").status_code == 201
    fake_clock.advance(600)
    assert send(client, "2026-10-01T09:00:00Z").status_code == 200  # the duplicate
    assert last_received_at(owner_engine) == T0 + timedelta(seconds=600)
    fake_clock.advance(899)  # 1,499 s after the first sample, 899 s after the duplicate
    assert gaps.sweep(app_engine) == []
    fake_clock.advance(1)
    assert len(gaps.sweep(app_engine)) == 1


@pytest.mark.parametrize(
    "timestamp",
    [
        "2020-01-01T00:00:00Z",
        "2026-09-30T09:00:00Z",
        "2026-10-02T09:00:00Z",
        "2031-01-01T00:00:00Z",
    ],
    ids=["years-old", "a-day-old", "a-day-ahead", "years-ahead"],
)
def test_wrong_sender_clock_neither_stops_nor_fakes_a_gap(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    timestamp: str,
) -> None:
    """Silence is measured from server receive time (spec 3.4 step 2)."""
    fake_clock.advance(600)
    assert send(client, timestamp).status_code == 201
    assert last_received_at(owner_engine) == T0 + timedelta(seconds=600)
    # Does not fake a gap: an old timestamp does not make the system look silent.
    fake_clock.advance(899)
    assert gaps.sweep(app_engine) == []
    # Does not stop a gap: a future timestamp does not keep the system looking alive.
    fake_clock.advance(1)
    [gap] = gaps.sweep(app_engine)
    assert rows(owner_engine, "SELECT state FROM event WHERE event_id = :e", e=gap) == [
        {"state": "FLAGGED"}
    ]


def test_recovery_through_real_ingest_closes_the_gap(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    fake_clock.advance(900)  # silent since the seed
    [first] = gaps.sweep(app_engine)
    fake_clock.advance(42)
    assert send(client, "2026-10-01T09:15:42Z").status_code == 201
    assert rows(owner_engine, "SELECT state FROM event WHERE event_id = :e", e=first) == [
        {"state": "CLOSED"}
    ]
    [closed] = rows(
        owner_engine, "SELECT payload FROM ledger_entry WHERE entry_type = 'GAP_CLOSED'"
    )
    assert closed["payload"] == {"event_id": first, "system_id": "sys-42", "gap_seconds": 42}
    fake_clock.advance(900)
    [second] = gaps.sweep(app_engine)
    assert second != first
    assert entry_types(owner_engine) == [
        "SEED",
        "GAP_OPENED",
        "NOTIFICATION_REQUESTED",
        "GAP_CLOSED",
        "GAP_OPENED",
        "NOTIFICATION_REQUESTED",
    ]
