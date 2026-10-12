"""M4: roles, check order and error bodies on the Part 2 endpoints.

Expected values are reasoned out in comments next to each assertion.
"""

import logging
import re
from typing import Any

import pytest
from sqlalchemy import Engine

from app import ledger
from app.ledger.canonical import LedgerError
from tests.api.conftest import (
    FORBIDDEN,
    INTERNAL,
    MALFORMED,
    NOT_FOUND,
    UNAUTHORIZED,
    ApiClient,
    append_lock_is_free,
    as_actor,
    build_app,
    count_entries,
    flag_event,
    owner_rows,
)

SECRET = "SECRET-payload-value-4419"
ENDPOINTS = ["/v1/events/evt-a/audit", "/v1/ledger/export", "/v1/ledger/verify"]


# F1. Roles


@pytest.mark.parametrize("path", ENDPOINTS)
def test_system_principal_is_forbidden(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, path: str
) -> None:
    flag_event(app_engine, "evt-a")
    before = owner_rows(owner_engine)
    response = client.get(path, headers=as_actor("sys-42"))
    # The system key is authenticated but its role is not REVIEWER or VIEWER: 403.
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert owner_rows(owner_engine) == before


@pytest.mark.parametrize("path", ENDPOINTS)
def test_missing_principal_is_unauthorized(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, path: str
) -> None:
    flag_event(app_engine, "evt-a")
    before = owner_rows(owner_engine)
    response = client.get(path)  # no X-Test-Actor: no principal
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    assert owner_rows(owner_engine) == before


@pytest.mark.parametrize("actor", ["rev-1", "viewer-1"])
@pytest.mark.parametrize("path", ENDPOINTS)
def test_reviewer_and_viewer_are_allowed(
    client: ApiClient, app_engine: Engine, path: str, actor: str
) -> None:
    flag_event(app_engine, "evt-a")
    assert client.get(path, headers=as_actor(actor)).status_code == 200


def test_the_default_stub_principal_is_rev_1(app_engine: Engine, owner_engine: Engine) -> None:
    flag_event(app_engine, "evt-a")
    with ApiClient(build_app(override_principal=False)) as client:
        response = client.get("/v1/events/evt-a/audit")
    # With no override, the stub resolver returns REVIEWER rev-1 (plan G3), so the call is
    # allowed and its AUDIT_VIEWED names rev-1.
    assert response.status_code == 200
    audit_rows = [r for r in owner_rows(owner_engine) if r["entry_type"] == "AUDIT_VIEWED"]
    assert [r["actor_id"] for r in audit_rows] == ["rev-1"]


# F2. 401 and 403 come before the 404 lookup


def test_auth_checks_come_before_the_event_lookup(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    flag_event(app_engine, "evt-a")
    before = owner_rows(owner_engine)
    path = "/v1/events/evt-missing/audit"
    # The event is unknown, but the role check runs first: 403 for the system key ...
    response = client.get(path, headers=as_actor("sys-42"))
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    # ... and the key check before that: 401 with no principal.
    response = client.get(path)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    # Only an allowed role reaches the lookup and gets 404.
    response = client.get(path, headers=as_actor("viewer-1"))
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert owner_rows(owner_engine) == before


# F3. Error bodies


@pytest.mark.parametrize(
    "path", ["/nope", "/v1/ledger", "/v1/ledger/verify/extra", "/v1/events/evt-a/audit/x"]
)
def test_unknown_route_is_404_not_found(client: ApiClient, path: str) -> None:
    response = client.get(path, headers=as_actor("viewer-1"))
    assert (response.status_code, response.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ENDPOINTS)
def test_wrong_method_is_404_not_found(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, path: str, method: str
) -> None:
    flag_event(app_engine, "evt-a")
    before = owner_rows(owner_engine)
    response = client.request(method, path, headers=as_actor("viewer-1"))
    # The spec has no 405 code, so a wrong method is NOT_FOUND (decision 27).
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert owner_rows(owner_engine) == before


def test_validation_failure_is_400_not_422(client: ApiClient) -> None:
    response = client.get("/_test/typed", params={"count": "not-a-number"})
    assert (response.status_code, response.json()) == (400, MALFORMED)
    response = client.get("/_test/typed")  # missing required parameter
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert client.get("/_test/typed", params={"count": "3"}).json() == {"count": 3}


def leaky_append(error: type[Exception]) -> Any:
    """ledger.append that really appends (taking the lock), then raises with a message that
    carries a payload value and the new entry's hash, which must never be shown or logged."""
    real_append = ledger.append

    def append(*args: Any, **kwargs: Any) -> ledger.LedgerEntry:
        entry = real_append(*args, **kwargs)
        raise error(f"{SECRET} {entry.hash}")

    return append


@pytest.mark.parametrize("error", [LedgerError, RuntimeError])
@pytest.mark.parametrize("path", ["/v1/events/evt-a/audit", "/v1/events/evt-a"])
def test_append_failure_is_a_generic_500_and_commits_nothing(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
    error: type[Exception],
) -> None:
    flag_event(app_engine, "evt-a")
    before = owner_rows(owner_engine)
    monkeypatch.setattr(ledger, "append", leaky_append(error))
    caplog.set_level(logging.DEBUG)

    response = client.get(path, headers=as_actor("viewer-1"))

    # The append happened inside the request's transaction, then failed: 500 with the generic
    # body, the transaction rolled back (no new row) and the lock released.
    assert (response.status_code, response.json()) == (500, INTERNAL)
    assert owner_rows(owner_engine) == before
    assert count_entries(owner_engine, "AUDIT_VIEWED") == 0
    assert count_entries(owner_engine, "EVENT_VIEWED") == 0
    assert append_lock_is_free(owner_engine)
    # Nothing secret in the body or in any log record: no payload value, no hash (existing
    # or the rolled-back one, which is 64 hex characters in the message), no traceback.
    assert SECRET not in response.text
    assert not re.search(r"[0-9a-f]{64}", response.text)
    for record in caplog.records:
        text = record.getMessage()
        assert SECRET not in text
        assert not re.search(r"[0-9a-f]{64}", text)
        assert "Traceback" not in text
        assert record.exc_info is None
        assert record.exc_text is None
    assert any(r.name == "api" and "unhandled exception" in r.getMessage() for r in caplog.records)
