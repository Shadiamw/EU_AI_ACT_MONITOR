"""Fixtures for the Part 3 (worker) tests.

- Every test starts with empty worker and stub tables, as well as the empty ledger the shared
  conftest gives it.
- `fake_clock` replaces app.clock.now with a clock the test moves by hand, as the simulated
  test clock does (spec, 'Clock (Pinned)').
- `receiver` is a real HTTP server on 127.0.0.1 that records every webhook call and answers
  with the statuses the test queues. A test can run a check inside the call with `during`.
  It lives in the root tests/conftest.py, so tests outside tests/worker can use it (Phase 4
  Addendum 9.1); Call, Receiver and receiver are re-exported here, so imports from this
  module keep working.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import clock
from app.platform import with_phase4_tables
from tests.conftest import Call as Call
from tests.conftest import Receiver as Receiver
from tests.conftest import receiver as receiver

WORKER_TABLES = (
    "notification_delivery",
    "notification",
    "webhook",
    "harm_field",
    "event",
    "sweeper_heartbeat",
    "monitored_system",
)

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def empty_worker_tables(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, WORKER_TABLES))}"))


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current += timedelta(seconds=seconds)
        return self.current

    def set(self, moment: datetime) -> None:
        self.current = moment


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(T0)
    monkeypatch.setattr(clock, "now", fake.now)
    return fake


def closed_port_url() -> str:
    """A URL on 127.0.0.1 where nothing listens, so a call gets no response."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_port
    server.server_close()
    return f"http://127.0.0.1:{port}/hook"


def add_system(
    owner_engine: Engine,
    system_id: str,
    *,
    last_received_at: datetime = T0,
    sampling_interval_s: int = 300,
    gap_timeout_intervals: int = 3,
) -> None:
    """A monitored_system row as Part 4's seed writes it (spec 4.4; sys-42: 300 s, 3)."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO monitored_system"
                " (system_id, sampling_interval_s, gap_timeout_intervals, last_received_at)"
                " VALUES (:system_id, :interval, :intervals, :last_received_at)"
            ),
            {
                "system_id": system_id,
                "interval": sampling_interval_s,
                "intervals": gap_timeout_intervals,
                "last_received_at": last_received_at,
            },
        )


def add_event(
    owner_engine: Engine,
    event_id: str = "evt-1",
    *,
    event_type: str = "threshold_breach",
    system_id: str = "sys-42",
    detected_at: datetime = T0,
    received_at: datetime = T0,
) -> None:
    """An event row as Part 1's ingest would write it, for notification tests."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event (event_id, system_id, type, detected_at, received_at, state,"
                " retain_until) VALUES (:event_id, :system_id, :type, :detected_at,"
                " :received_at, 'FLAGGED', :retain_until)"
            ),
            {
                "event_id": event_id,
                "system_id": system_id,
                "type": event_type,
                "detected_at": detected_at,
                "received_at": received_at,
                "retain_until": detected_at + timedelta(days=200),
            },
        )


def add_webhook(owner_engine: Engine, webhook_id: str, url: str, event_types: list[str]) -> None:
    """A webhook row without the API's ping or ledger entry, for delivery tests."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO webhook (webhook_id, url, event_types, created_at)"
                " VALUES (:webhook_id, :url, :event_types, :now)"
            ),
            {"webhook_id": webhook_id, "url": url, "event_types": event_types, "now": T0},
        )


def rows(owner_engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with owner_engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql), params)]


def entry_types(owner_engine: Engine) -> list[str]:
    return [
        r["entry_type"]
        for r in rows(owner_engine, "SELECT entry_type FROM ledger_entry ORDER BY seq")
    ]


def app_sessions_in_transaction(owner_engine: Engine) -> int:
    """Application-role connections that have a transaction open.

    pg_stat_activity hides xact_start from other roles, but every open transaction holds its
    own virtualxid lock, and pg_locks is visible to all.
    """
    return _count_app_locks(owner_engine, "l.locktype = 'virtualxid' AND l.granted")


def app_sessions_waiting_for_a_lock(owner_engine: Engine) -> int:
    return _count_app_locks(owner_engine, "NOT l.granted")


def _count_app_locks(owner_engine: Engine, condition: str) -> int:
    with owner_engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(DISTINCT l.pid) FROM pg_locks l"
                    " JOIN pg_stat_activity a ON a.pid = l.pid"
                    f" WHERE a.usename = 'monitor_app' AND {condition}"
                )
            ).scalar_one()
        )


@pytest.fixture(scope="session", autouse=True)
def dispose_app_engine() -> Iterator[None]:
    """The API tests here use the app's cached, pooled engine; close it at the end."""
    yield
    from app.db import get_engine

    get_engine().dispose()
