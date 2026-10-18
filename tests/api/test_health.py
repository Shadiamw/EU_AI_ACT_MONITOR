"""GET /healthz (decision 111)."""

from sqlalchemy import Engine, text

from app.main import create_app
from tests.api.conftest import ApiClient


def _clear(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE notification_delivery, notification, sweeper_heartbeat"))


def test_healthz_before_the_first_sweep_is_200(owner_engine: Engine) -> None:
    _clear(owner_engine)
    with ApiClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "sweeper_last_ran_seconds_ago": None,
        "pending_deliveries": 0,
        "oldest_pending_delivery_seconds": None,
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
