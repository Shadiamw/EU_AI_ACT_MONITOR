"""Phase 5 contract delta (spec 9.2): every new or changed endpoint exists on the stack. Without a
key each answers 401 MISSING_OR_INVALID_KEY, never 404 or 405 (spec 10.1).

Δ1 and Δ2 (the Report object and GET …/report) share the report route; Δ7 and Δ8 are error codes
and a payload key, checked by the AC-13, AC-18 and Model 5 tests; Δ9 is the clock-advance bound
of the Phase 4 route; Δ11 is a verifier option, not an endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from tests.acceptance.api import MISSING_KEY

pytestmark = pytest.mark.integration

DELTA_ROUTES = [
    ("Δ2", "GET", "/v1/events/evt-unknown/report"),
    ("Δ2", "GET", "/v1/events/evt-unknown/report?format=pdf"),
    ("Δ3", "POST", "/v1/events/evt-unknown/report/submission"),
    ("Δ4", "GET", "/v1/events/evt-unknown/reports"),
    ("Δ4", "GET", "/v1/reports/rpt-unknown"),
    ("Δ5", "GET", "/v1/ledger/verifications"),
    ("Δ5", "POST", "/v1/ledger/verifications"),
    ("Δ6", "POST", "/v1/events/evt-unknown/erasure"),
    ("Δ10", "GET", "/v1/ops/dashboard"),
]


@pytest.mark.parametrize(
    ("delta", "method", "path"),
    DELTA_ROUTES,
    ids=[f"{delta} {method} {path}" for delta, method, path in DELTA_ROUTES],
)
def test_contract_delta_routes_exist(
    http: httpx.Client, delta: str, method: str, path: str
) -> None:
    response = http.request(method, path)
    assert response.status_code == 401, f"{delta}: {response.status_code} {response.text}"
    assert response.json() == MISSING_KEY
