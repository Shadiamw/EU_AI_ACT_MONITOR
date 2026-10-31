"""HTTP acceptance for Part 4 test endpoints (spec 4.5). Integration-marked (Addendum rule 10)."""

from __future__ import annotations

import httpx
import pytest

from tests.acceptance.conftest import bearer

pytestmark = pytest.mark.integration


def test_reset_is_204_with_a_reviewer_key(http: httpx.Client, keys: dict[str, str]) -> None:
    response = http.post("/v1/test/reset", headers=bearer(keys, "reviewer"))
    assert response.status_code == 204
    assert response.content == b""


def test_clock_advance_returns_only_simulated_time(
    http: httpx.Client, keys: dict[str, str]
) -> None:
    assert http.post("/v1/test/reset", headers=bearer(keys, "reviewer")).status_code == 204
    response = http.post(
        "/v1/test/clock/advance",
        headers=bearer(keys, "viewer"),
        json={"seconds": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"simulated_time"}
    assert body["simulated_time"].endswith("Z")


def test_suite_requires_reviewer(http: httpx.Client, keys: dict[str, str]) -> None:
    denied = http.post(
        "/v1/test/suite",
        headers=bearer(keys, "viewer"),
        json={"scope": "smoke"},
    )
    assert denied.status_code == 403
