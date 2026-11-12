"""Phase 5 Part 3, M6: /healthz reports the build version (spec 6.5, D15; decisions 295, 300).

version comes from APP_VERSION (set at image build from the Dockerfile's ARG GIT_SHA) and is null
when unset or empty. The same test checks the other three new fields on a running app: the
latest verification run and its age on the wall clock (patched as app.clock.wall_now), and
reset_count after two resets. Expected values come from the spec, not from output.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app import clock
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer

WALL = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)


def healthz(client: ApiClient) -> Any:
    response = client.get("/healthz")  # no key: /healthz is unauthenticated
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz_reports_the_app_version(
    client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_VERSION", "abc123")
    body = healthz(client)
    assert body["version"] == "abc123"
    monkeypatch.delenv("APP_VERSION")
    assert healthz(client)["version"] is None
    monkeypatch.setenv("APP_VERSION", "")  # a build without --build-arg GIT_SHA
    assert healthz(client)["version"] is None

    # Before the first run: no verification, and no reset has been counted.
    assert (
        body["last_verification"],
        body["last_verification_age_seconds"],
        body["reset_count"],
    ) == (None, None, 0)

    # After a run: its finished_at, valid and head_check, aged on the wall clock.
    monkeypatch.setattr(clock, "wall_now", lambda: WALL)
    response = client.request("POST", "/v1/ledger/verifications", headers=bearer("rev-1"))
    assert response.status_code == 201, response.text
    monkeypatch.setattr(clock, "wall_now", lambda: WALL + timedelta(hours=1))
    body = healthz(client)
    assert body["last_verification"] == {
        "finished_at": "2026-11-12T03:00:00Z",
        "valid": True,
        "head_check": "FIRST_RUN",
    }
    assert body["last_verification_age_seconds"] == 3600

    # Two resets raise reset_count by exactly two.
    for _ in range(2):
        response = client.request("POST", "/v1/test/reset", headers=bearer("rev-1"))
        assert response.status_code == 204
    assert healthz(client)["reset_count"] == 2
