"""Part 4: POST /v1/test/reset and POST /v1/test/clock/advance (decision 133)."""

import pytest
from sqlalchemy import Engine, text

from app.main import create_app
from tests.api.conftest import NOT_FOUND, ApiClient
from tests.db_helpers import bearer


def test_reset_leaves_a_valid_seed_and_zero_offset(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        response = client.request("POST", "/v1/test/reset")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        verify = client.get("/v1/ledger/verify", headers=bearer("viewer-1"))
    assert verify.json() == {"valid": True, "entries_checked": 1, "first_mismatch": None}
    with owner_engine.connect() as conn:
        offset = conn.execute(text("SELECT offset_seconds FROM clock_state")).scalar_one()
        systems = conn.execute(text("SELECT system_id FROM monitored_system")).scalars().all()
    assert offset == 0
    assert systems == ["sys-42"]


def test_clock_advance_opens_a_gap_after_three_intervals(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset").status_code == 200
        body = client.request("POST", "/v1/test/clock/advance", json={"seconds": 900}).json()
    assert body["ok"] is True
    assert body["seconds_advanced"] == 900
    assert body["offset_seconds"] == 900
    assert len(body["gaps_opened"]) == 1
    with owner_engine.connect() as conn:
        types = (
            conn.execute(text("SELECT type FROM event WHERE type = 'monitoring_gap'"))
            .scalars()
            .all()
        )
    assert types == ["monitoring_gap"]


def test_test_routes_are_404_outside_the_test_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset").json() == NOT_FOUND
        assert (
            client.request("POST", "/v1/test/clock/advance", json={"seconds": 1}).json()
            == NOT_FOUND
        )
