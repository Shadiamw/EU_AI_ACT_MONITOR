"""Every project route is in the HTTP role matrix and is either a Phase 4 route or a Phase 5
contract-delta route (spec 9.2 and 10.2; audit F-09).

The project routes are the app's OpenAPI paths, without the cross-team harness (/system and
/tester, which have their own interface and tests) and without the routes in EXEMPT. Adding or
removing a route without updating the role matrix (tests/acceptance/test_ac14_role_matrix.py),
the contract test's DELTA_ROUTES (tests/acceptance/test_phase5_contract.py) and PHASE4_ROUTES
below fails here.
"""

from __future__ import annotations

import re

from app.main import create_app
from tests.acceptance.test_ac14_role_matrix import ROUTES
from tests.acceptance.test_phase5_contract import DELTA_ROUTES

# Routes outside the role matrix, each with its reason.
EXEMPT = {
    ("GET", "/"): "the console redirect; no key, no data",
    ("GET", "/healthz"): "no key by design (spec 7.4: 200 for every caller); runner AC-06 tests",
    ("GET", "/livez"): "liveness probe, no key, no data",
    ("GET", "/pod_spec.txt"): "the published pod contract text, no key",
}

# The Phase 4 project routes (main d9205f2, before Phase 5), in the matrix's {id} notation.
PHASE4_ROUTES = {
    ("POST", "/v1/metrics"),
    ("GET", "/v1/events"),
    ("GET", "/v1/events/{id}"),
    ("POST", "/v1/events/{id}/disposition"),
    ("GET", "/v1/events/{id}/report"),
    ("GET", "/v1/events/{id}/audit"),
    ("GET", "/v1/ledger/export"),
    ("GET", "/v1/ledger/verify"),
    ("POST", "/v1/webhooks"),
    ("POST", "/v1/test/clock/advance"),
    ("POST", "/v1/test/reset"),
    ("GET", "/v1/test/suite"),
    ("POST", "/v1/test/suite"),
}


def _template(path: str) -> str:
    path = path.split("?", 1)[0]
    path = re.sub(r"\{[a-z_]+\}", "{id}", path)
    return path.replace("evt-unknown", "{id}").replace("rpt-unknown", "{id}")


def _app_routes() -> set[tuple[str, str]]:
    paths = create_app().openapi()["paths"]
    return {
        (method.upper(), _template(path))
        for path, operations in paths.items()
        for method in operations
        if not path.startswith(("/system", "/tester"))
    }


def test_every_project_route_is_in_the_role_matrix_and_the_contract_inventory() -> None:
    app_routes = _app_routes()
    assert set(EXEMPT) <= app_routes, "an exempt route no longer exists"
    project = app_routes - set(EXEMPT)
    matrix = {tuple(route.name.split(" ", 1)) for route in ROUTES}
    assert len(matrix) == len(ROUTES)
    assert project == matrix
    delta = {(method, _template(path)) for _, method, path in DELTA_ROUTES}
    assert project == PHASE4_ROUTES | delta
    assert not set(EXEMPT) & matrix
