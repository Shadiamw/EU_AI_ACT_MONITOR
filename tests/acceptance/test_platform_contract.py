"""HTTP acceptance for Part 4 test endpoints (spec 4.5). Integration-marked (Addendum rule 10).

Each test starts after the autouse reset in conftest.py."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_reset_is_204_with_a_reviewer_key(
    http: httpx.Client, headers: dict[str, dict[str, str]]
) -> None:
    response = http.post("/v1/test/reset", headers=headers["reviewer"])
    assert response.status_code == 204
    assert response.content == b""


def test_clock_advance_returns_only_simulated_time(
    http: httpx.Client, headers: dict[str, dict[str, str]]
) -> None:
    response = http.post(
        "/v1/test/clock/advance",
        headers=headers["viewer"],
        json={"seconds": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"simulated_time"}
    assert body["simulated_time"].endswith("Z")


def test_suite_requires_reviewer(http: httpx.Client, headers: dict[str, dict[str, str]]) -> None:
    denied = http.post(
        "/v1/test/suite",
        headers=headers["viewer"],
        json={"scope": "smoke"},
    )
    assert denied.status_code == 403
