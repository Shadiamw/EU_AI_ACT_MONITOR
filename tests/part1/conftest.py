"""Fixtures for the Part 1 tests: the real app, real keys and a real PostgreSQL database.

- Every test starts from the spec's seed fixtures (spec 4, "Seed fixtures"), installed by the
  Part 4 stand-in tools/seed_part1.py from the keys in the environment: sys-42 (300 s, 3
  intervals), thr-1 accuracy_drift gt 0.30 and thr-2 accuracy lt 0.70 (version 1), the three
  key digests and the SEED ledger entry.
- The clock is the fake clock of tests/worker/conftest.py, started at the timestamp of contract
  Example 1 (2026-09-27T10:05:00Z), so the seed's last_received_at is that moment too.
- Requests carry a real `Authorization: Bearer <key>` header (spec 4, Auth); nothing overrides
  the principal resolver.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, observability
from app.main import create_app
from app.platform import with_phase4_tables
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.worker.conftest import FakeClock
from tests.worker.conftest import receiver as receiver
from tools.seed_part1 import seed

# Contract Example 1's timestamp.
T0 = datetime(2026, 9, 27, 10, 5, tzinfo=UTC)

# Tables the root conftest does not empty (Part 3's webhook, truncated with the delivery rows
# that reference it), and the system rows the seed writes.
OTHER_TABLES = (
    "notification_delivery",
    "webhook",
    "sweeper_heartbeat",
    "monitored_system",
    "clock_state",
)

# Contract Example 1, request body.
EXAMPLE_1 = {
    "system_id": "sys-42",
    "metric_name": "accuracy_drift",
    "metric_type": "drift",
    "value": 0.34,
    "timestamp": "2026-09-27T10:05:00Z",
}


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(T0)
    monkeypatch.setattr(clock, "now", fake.now)
    return fake


@pytest.fixture
def seeded(owner_engine: Engine, fake_clock: FakeClock) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, OTHER_TABLES))}"))
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    observability.reset_counters()


@pytest.fixture
def client(seeded: None) -> Iterator[ApiClient]:
    with ApiClient(create_app()) as api_client:
        yield api_client


def metric(**changes: Any) -> dict[str, Any]:
    """Contract Example 1 with some fields changed."""
    return {**EXAMPLE_1, **changes}


def post(client: ApiClient, actor: str = "sys-42", **changes: Any) -> httpx.Response:
    return client.request("POST", "/v1/metrics", headers=bearer(actor), json=metric(**changes))


def post_raw(client: ApiClient, content: str, actor: str = "sys-42") -> httpx.Response:
    """A body sent exactly as written, so JSON numbers keep their digits (0.30, NaN, ...)."""
    return client.request(
        "POST",
        "/v1/metrics",
        headers={**bearer(actor), "Content-Type": "application/json"},
        content=content,
    )


def get(client: ApiClient, url: str, actor: str = "viewer-1") -> httpx.Response:
    return client.get(url, headers=bearer(actor))


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql), params)]


def entry_types(owner_engine: Engine) -> list[str]:
    return [
        r["entry_type"]
        for r in rows(owner_engine, "SELECT entry_type FROM ledger_entry ORDER BY seq")
    ]
