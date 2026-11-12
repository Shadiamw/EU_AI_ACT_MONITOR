"""GET /healthz (decision 111)."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from app import worker
from app.main import create_app
from app.platform import with_phase4_tables
from tests.api.conftest import ApiClient


def _clear(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        tables = ("notification_delivery", "notification", "sweeper_heartbeat")
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, tables))}"))


def test_healthz_before_the_first_sweep_is_200(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear(owner_engine)
    monkeypatch.delenv("APP_VERSION", raising=False)
    with ApiClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "sweeper_last_ran_seconds_ago": None,
        "pending_deliveries": 0,
        "oldest_pending_delivery_seconds": None,
        "overdue_escalations": 0,
        "version": None,
        "last_verification": None,
        "last_verification_age_seconds": None,
        "reset_count": 0,
    }


def test_healthz_is_503_when_the_last_sweep_is_older_than_three_intervals(
    owner_engine: Engine,
) -> None:
    _clear(owner_engine)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sweeper_heartbeat (last_ran_at)"
                " VALUES (CURRENT_TIMESTAMP - interval '1 hour')"
            )
        )
    with ApiClient(create_app()) as client:
        response = client.get("/healthz")
    body = response.json()
    assert response.status_code == 503
    assert body["pending_deliveries"] == 0
    assert body["oldest_pending_delivery_seconds"] is None
    assert body["sweeper_last_ran_seconds_ago"] > 3 * 60


def test_healthz_is_503_with_the_body_when_the_database_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIM-09: an unreachable database is 503 with the usual body shape, never a 500."""

    def unreachable(engine: Engine) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(worker, "health_values", unreachable)
    monkeypatch.setenv("APP_VERSION", "abc123")
    with ApiClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {
        "sweeper_last_ran_seconds_ago": None,
        "pending_deliveries": 0,
        "oldest_pending_delivery_seconds": None,
        "overdue_escalations": 0,
        "version": "abc123",  # from the environment; the other three new fields null (306)
        "last_verification": None,
        "last_verification_age_seconds": None,
        "reset_count": None,
    }


def test_healthz_needs_no_key_and_is_not_a_test_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with ApiClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code in (200, 503)
    assert set(response.json()) == {
        "sweeper_last_ran_seconds_ago",
        "pending_deliveries",
        "oldest_pending_delivery_seconds",
        "overdue_escalations",
        "version",
        "last_verification",
        "last_verification_age_seconds",
        "reset_count",
    }
