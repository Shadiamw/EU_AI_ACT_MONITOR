"""Real-key, real-PostgreSQL fixtures for Part 1."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock
from app.main import create_app
from tests.api.conftest import ApiClient
from tests.worker.conftest import receiver as receiver
from tools.seed_part1 import TEST_KEYS, seed

T0 = datetime(2026, 10, 17, 12, tzinfo=UTC)


def headers(role: str = "SYSTEM") -> dict[str, str]:
    return {"X-API-Key": TEST_KEYS[role]}


@pytest.fixture
def client(owner_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiClient]:
    monkeypatch.setattr(clock, "now", lambda: T0)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE notification_delivery, notification, webhook, monitoring_gap,"
                " monitored_system"
            )
        )
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    with ApiClient(create_app()) as client:
        yield client
