"""Part 4: POST /v1/test/reset and POST /v1/test/clock/advance (Phase 4 spec 4.5)."""

import pytest
from sqlalchemy import Engine, text

from app.main import create_app
from tests.api.conftest import NOT_FOUND, ApiClient
from tests.db_helpers import bearer


def test_reset_leaves_a_valid_seed_and_zero_offset(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        response = client.request("POST", "/v1/test/reset", headers=bearer("rev-1"))
        assert response.status_code == 204
        assert response.content == b""
        verify = client.get("/v1/ledger/verify", headers=bearer("viewer-1"))
    assert verify.json() == {"valid": True, "entries_checked": 1, "first_mismatch": None}
    with owner_engine.connect() as conn:
        offset = conn.execute(text("SELECT offset_seconds FROM clock_state")).scalar_one()
        systems = conn.execute(text("SELECT system_id FROM monitored_system")).scalars().all()
    assert offset == 0
    assert systems == ["sys-42"]


def test_clock_advance_opens_a_gap_after_three_intervals(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset", headers=bearer("rev-1")).status_code == 204
        response = client.request(
            "POST",
            "/v1/test/clock/advance",
            headers=bearer("viewer-1"),
            json={"seconds": 900},
        )
        body = response.json()
    assert response.status_code == 200
    assert set(body) == {"simulated_time"}
    assert isinstance(body["simulated_time"], str) and body["simulated_time"].endswith("Z")
    with owner_engine.connect() as conn:
        types = (
            conn.execute(text("SELECT type FROM event WHERE type = 'monitoring_gap'"))
            .scalars()
            .all()
        )
    assert types == ["monitoring_gap"]


def test_clock_advance_allows_zero_seconds() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset", headers=bearer("rev-1")).status_code == 204
        response = client.request(
            "POST",
            "/v1/test/clock/advance",
            headers=bearer("rev-1"),
            json={"seconds": 0},
        )
    assert response.status_code == 200
    assert set(response.json()) == {"simulated_time"}


def test_test_routes_require_a_key() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset").status_code == 401
        assert (
            client.request("POST", "/v1/test/clock/advance", json={"seconds": 1}).status_code == 401
        )


def test_test_routes_are_404_outside_the_test_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with ApiClient(create_app()) as client:
        # 404 before any key check (Addendum rule 7): no Authorization header.
        assert client.request("POST", "/v1/test/reset").json() == NOT_FOUND
        assert (
            client.request("POST", "/v1/test/clock/advance", json={"seconds": 1}).json()
            == NOT_FOUND
        )


def test_gap_events_are_listed_through_the_events_query() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset", headers=bearer("rev-1")).status_code == 204
        client.request(
            "POST",
            "/v1/test/clock/advance",
            headers=bearer("rev-1"),
            json={"seconds": 900},
        )
        gaps = client.get("/v1/events?type=monitoring_gap", headers=bearer("viewer-1")).json()
    assert len(gaps) == 1
    assert gaps[0]["type"] == "monitoring_gap"
