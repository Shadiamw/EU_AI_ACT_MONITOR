"""AC-14 (REQ-13): severity assignment and report generation are restricted to the reviewer
role; one table-driven test covers every endpoint x {no key, unknown key, system, viewer,
reviewer} (spec 4.8).

Role matrix, spec section 7 and Addendum rules 6 and 7: POST /v1/metrics SYSTEM only;
GET /v1/events any role; POST .../disposition and GET .../report REVIEWER only; GET and
POST /v1/test/suite REVIEWER only; every other endpoint REVIEWER or VIEWER. Wrong role: 403
FORBIDDEN_ROLE; missing or unknown key: 401 MISSING_OR_INVALID_KEY. An allowed caller gets the
endpoint's own answer, listed per row. D1: the reviewer's report passes the role check (409
before a disposition, 501 after); "reviewer succeeds" for reports completes in Phase 5.

The suite row posts {"scope": "full"}, which is 400 for the reviewer, so no run starts. The
webhook row uses an address that refuses connections, so the allowed answer is 422.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from tests.acceptance.api import FORBIDDEN, MISSING_KEY, Api, decisions

pytestmark = pytest.mark.integration

ROLES = ("none", "unknown", "system", "viewer", "reviewer")
UNKNOWN_KEY = {"Authorization": "Bearer not-a-key-of-this-stack-0000000000000000"}


@dataclass(frozen=True)
class Route:
    name: str
    call: Callable[[Api, dict[str, str], str], httpx.Response]
    allowed: dict[str, int]  # role -> status for an allowed caller


def _metrics(api: Api, headers: dict[str, str], _event: str) -> httpx.Response:
    body = {
        "system_id": "sys-42",
        "metric_name": "accuracy",
        "metric_type": "performance",
        "value": 0.9,
        "timestamp": "2026-10-05T09:00:00Z",
    }
    return api.http.post("/v1/metrics", headers=headers, json=body)


def _get(path: str) -> Callable[[Api, dict[str, str], str], httpx.Response]:
    def call(api: Api, headers: dict[str, str], event_id: str) -> httpx.Response:
        return api.http.get(path.format(event_id=event_id), headers=headers)

    return call


def _post(path: str, body: Any) -> Callable[[Api, dict[str, str], str], httpx.Response]:
    def call(api: Api, headers: dict[str, str], event_id: str) -> httpx.Response:
        return api.http.post(path.format(event_id=event_id), headers=headers, json=body)

    return call


READERS = {"viewer": 200, "reviewer": 200}
ROUTES = [
    Route("POST /v1/metrics", _metrics, {"system": 201}),
    Route("GET /v1/events", _get("/v1/events"), {"system": 200, "viewer": 200, "reviewer": 200}),
    Route("GET /v1/events/{id}", _get("/v1/events/{event_id}"), READERS),
    Route(
        "POST /v1/events/{id}/disposition",
        _post(
            "/v1/events/{event_id}/disposition",
            {"tier": "OTHER_SERIOUS", "rationale": None, "harm_fields": decisions()},
        ),
        {"reviewer": 200},
    ),
    Route("GET /v1/events/{id}/report", _get("/v1/events/{event_id}/report"), {"reviewer": 409}),
    Route("GET /v1/events/{id}/audit", _get("/v1/events/{event_id}/audit"), READERS),
    Route("GET /v1/ledger/export", _get("/v1/ledger/export"), READERS),
    Route("GET /v1/ledger/verify", _get("/v1/ledger/verify"), READERS),
    Route(
        "POST /v1/webhooks",
        _post("/v1/webhooks", {"url": "http://127.0.0.1:9/refused", "event_types": ["reminder"]}),
        {"viewer": 422, "reviewer": 422},
    ),
    Route(
        "POST /v1/test/clock/advance",
        _post("/v1/test/clock/advance", {"seconds": 0}),
        READERS,
    ),
    Route("POST /v1/test/reset", _post("/v1/test/reset", None), {"viewer": 204, "reviewer": 204}),
    Route("GET /v1/test/suite", _get("/v1/test/suite"), {"reviewer": 200}),
    Route("POST /v1/test/suite", _post("/v1/test/suite", {"scope": "full"}), {"reviewer": 400}),
]
CASES = [
    pytest.param(route, role, id=f"{route.name} as {role}") for route in ROUTES for role in ROLES
]


def _headers(api: Api, role: str) -> dict[str, str]:
    if role == "none":
        return {}
    if role == "unknown":
        return UNKNOWN_KEY
    return api.headers[role]


@pytest.mark.parametrize(("route", "role"), CASES)
def test_role_matrix(api: Api, route: Route, role: str) -> None:
    event_id, _ = api.flag()
    before = api.entries()
    response = route.call(api, _headers(api, role), event_id)

    if role in route.allowed:
        assert response.status_code == route.allowed[role], (
            f"{response.status_code} {response.text}"
        )
        return
    expected, body = (401, MISSING_KEY) if role in ("none", "unknown") else (403, FORBIDDEN)
    assert response.status_code == expected, f"{response.status_code} {response.text}"
    assert response.json() == body
    assert api.entries() == before  # a denied request appends nothing (spec 7)


@pytest.mark.parametrize("role", ["viewer", "system"])
def test_a_non_reviewer_is_denied_both_actions_and_the_reviewer_is_not(api: Api, role: str) -> None:
    event_id, _ = api.flag()
    api.event(event_id, role="reviewer")  # UNDER_REVIEW
    before = api.entries()

    denied = api.dispose(event_id, "OTHER_SERIOUS", role=role)
    assert denied.status_code == 403, denied.text
    assert denied.json() == FORBIDDEN
    report = api.report(event_id, role=role)
    assert report.status_code == 403, report.text
    assert report.json() == FORBIDDEN
    assert api.entries() == before
    assert api.event(event_id)["state"] == "UNDER_REVIEW"

    assigned = api.disposed(event_id, "OTHER_SERIOUS")
    assert assigned["state"] == "DISPOSITIONED"
    assert assigned["disposition"]["reviewer_id"] == "rev-1"
    assert api.report(event_id).status_code == 501  # passes the guard; the report is Phase 5
