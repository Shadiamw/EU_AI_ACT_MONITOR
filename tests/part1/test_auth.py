"""Part 1: authentication, roles and the order of checks (spec 1.3, spec 4 Auth and Role
matrix, spec 1.9 Auth matrix and Stubs; decision 123).
"""

import hashlib
import hmac
import logging
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import config
from tests.api.conftest import FORBIDDEN, MALFORMED, UNAUTHORIZED, ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import entry_types, metric, post, post_raw, rows

ACTORS = ("sys-42", "rev-1", "viewer-1")


def env_keys() -> dict[str, str]:
    return {
        "sys-42": config.ingest_key_sys_42(),
        "rev-1": config.reviewer_key(),
        "viewer-1": config.viewer_key(),
    }


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
def test_bearer_key_is_accepted(client: ApiClient, actor: str, scheme: str) -> None:
    """GET /v1/events accepts any role (spec 4). The scheme name is case-insensitive
    (RFC 9110; decision 123)."""
    headers = {"Authorization": f"{scheme} {env_keys()[actor]}"}
    assert client.get("/v1/events", headers=headers).status_code == 200


@pytest.mark.parametrize("actor", ACTORS)
def test_x_api_key_header_is_not_accepted(client: ApiClient, actor: str) -> None:
    response = client.get("/v1/events", headers={"X-API-Key": env_keys()[actor]})
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


def malformed_headers() -> dict[str, dict[str, str]]:
    key = config.ingest_key_sys_42()
    return {
        "none": {},
        "unknown-key": {"Authorization": "Bearer not-a-seeded-key"},
        "empty-key": {"Authorization": "Bearer "},
        "scheme-only": {"Authorization": "Bearer"},
        "basic-scheme": {"Authorization": f"Basic {key}"},
        "no-scheme": {"Authorization": key},
        "two-tokens": {"Authorization": f"Bearer {key} extra"},
        "key-prefix": {"Authorization": f"Bearer {key[:-1]}"},
        "the-stored-digest": {
            "Authorization": f"Bearer {hashlib.sha256(key.encode()).hexdigest()}"
        },
        "too-long": {"Authorization": "Bearer " + "k" * 5000},
    }


@pytest.mark.parametrize("case", malformed_headers().keys())
def test_missing_unknown_or_malformed_authorization_is_401(
    client: ApiClient, owner_engine: Engine, case: str
) -> None:
    # A broken body as well: 401 comes before body validation (spec 4).
    response = client.request(
        "POST", "/v1/metrics", headers=malformed_headers()[case], content="not json"
    )
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert entry_types(owner_engine) == ["SEED"]


def test_principals_have_the_seeded_actor_ids(client: ApiClient, owner_engine: Engine) -> None:
    """sys-42, rev-1 and viewer-1 appear as actor ids in the ledger (spec 4, Actor ids)."""
    event_id = post(client).json()["flagged_event_id"]
    for actor in ("rev-1", "viewer-1"):
        assert client.get(f"/v1/events/{event_id}", headers=bearer(actor)).status_code == 200
    assert rows(
        owner_engine,
        "SELECT entry_type, actor_id FROM ledger_entry WHERE entry_type IN"
        " ('EVENT_FLAGGED', 'EVENT_VIEWED') ORDER BY seq",
    ) == [
        {"entry_type": "EVENT_FLAGGED", "actor_id": "sys-42"},
        {"entry_type": "EVENT_VIEWED", "actor_id": "rev-1"},
        {"entry_type": "EVENT_VIEWED", "actor_id": "viewer-1"},
    ]


def test_keys_are_stored_only_as_sha256_digests(client: ApiClient, owner_engine: Engine) -> None:
    stored = rows(
        owner_engine, "SELECT key_hash, role, actor_id, system_id FROM api_key ORDER BY actor_id"
    )
    keys = env_keys()
    assert stored == [
        {
            "key_hash": hashlib.sha256(keys["rev-1"].encode("utf-8")).hexdigest(),
            "role": "REVIEWER",
            "actor_id": "rev-1",
            "system_id": None,
        },
        {
            "key_hash": hashlib.sha256(keys["sys-42"].encode("utf-8")).hexdigest(),
            "role": "SYSTEM",
            "actor_id": "sys-42",
            "system_id": "sys-42",
        },
        {
            "key_hash": hashlib.sha256(keys["viewer-1"].encode("utf-8")).hexdigest(),
            "role": "VIEWER",
            "actor_id": "viewer-1",
            "system_id": None,
        },
    ]
    # No key appears anywhere in the database's text columns of api_key.
    dump = repr(rows(owner_engine, "SELECT * FROM api_key"))
    assert not any(key in dump for key in keys.values())


@pytest.mark.parametrize("presented", ["viewer-1", "rev-1", "sys-42", "unknown"])
def test_keys_are_compared_with_compare_digest_against_every_stored_key(
    client: ApiClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch, presented: str
) -> None:
    """Constant time (spec 1.3): every stored digest goes through hmac.compare_digest, so the
    work does not depend on which key, if any, matches (decision 123)."""
    with owner_engine.begin() as conn:
        for i in range(5):
            conn.execute(
                text(
                    "INSERT INTO api_key (key_hash, role, actor_id, system_id)"
                    " VALUES (:h, 'VIEWER', :a, NULL)"
                ),
                {"h": hashlib.sha256(f"extra-{i}".encode()).hexdigest(), "a": f"extra-{i}"},
            )
    calls: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def counting(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return bool(real(a, b))

    monkeypatch.setattr(hmac, "compare_digest", counting)
    key = env_keys().get(presented, "unknown-key")
    response = client.get("/v1/events", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == (401 if presented == "unknown" else 200)
    assert len(calls) == 8  # the three seeded digests and the five extra ones


def test_keys_never_appear_in_logs(client: ApiClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    keys = env_keys()
    post(client)
    post(client, actor="rev-1")
    post(client, system_id="sys-99")
    client.get("/v1/events", headers=bearer("viewer-1"))
    client.get("/v1/events", headers={"Authorization": "Bearer wrong-key-123456"})
    client.get("/v1/no-such-route", headers=bearer("rev-1"))
    assert caplog.records  # the request log lines are there
    text_logged = caplog.text + repr([r.__dict__ for r in caplog.records])
    for secret in [*keys.values(), "wrong-key-123456"]:
        assert secret not in text_logged


# The role matrix (spec 4): (method, path) -> the roles allowed. Disposition and report are
# the 501 stubs (spec 1.6); everything else answers 200 for an allowed role.
ROUTES = {
    ("POST", "/v1/metrics"): {"sys-42"},
    ("GET", "/v1/events"): {"sys-42", "rev-1", "viewer-1"},
    ("GET", "/v1/events/{id}"): {"rev-1", "viewer-1"},
    ("GET", "/v1/events/{id}/audit"): {"rev-1", "viewer-1"},
    ("GET", "/v1/ledger/export"): {"rev-1", "viewer-1"},
    ("GET", "/v1/ledger/verify"): {"rev-1", "viewer-1"},
    ("POST", "/v1/events/{id}/disposition"): {"rev-1"},
    ("GET", "/v1/events/{id}/report"): {"rev-1", "viewer-1"},
}
SUCCESS = {("POST", "/v1/metrics"): 201}
STUBS = {("POST", "/v1/events/{id}/disposition"), ("GET", "/v1/events/{id}/report")}


@pytest.mark.parametrize("actor", [*ACTORS, None])
@pytest.mark.parametrize("route", ROUTES.keys(), ids=[f"{m} {p}" for m, p in ROUTES])
def test_role_matrix(client: ApiClient, route: tuple[str, str], actor: str | None) -> None:
    event_id = post(client).json()["flagged_event_id"]
    method, path = route
    url = path.replace("{id}", event_id)
    headers = {} if actor is None else bearer(actor)
    body = metric(timestamp="2026-09-27T10:10:00Z") if method == "POST" else None
    response = client.request(method, url, headers=headers, json=body)
    expected: tuple[int, dict[str, str] | None]
    if actor is None:
        expected = (401, UNAUTHORIZED)
    elif actor not in ROUTES[route]:
        expected = (403, FORBIDDEN)
    elif route in STUBS:
        expected = (501, None)
    else:
        expected = (SUCCESS.get(route, 200), None)
    assert response.status_code == expected[0]
    if expected[1] is not None:
        assert response.json() == expected[1]
    if expected[0] == 501:
        assert set(response.json()) == {"error_code", "message"}
        assert response.json()["error_code"] == "NOT_IMPLEMENTED"


def test_check_order_401_403_400_mismatch(client: ApiClient, owner_engine: Engine) -> None:
    """Spec 4: 401, then 403 FORBIDDEN_ROLE, then 400, then 403 SYSTEM_MISMATCH."""
    bad_and_foreign = '{"system_id": "sys-99", "value": "abc"}'
    response = client.request("POST", "/v1/metrics", content=bad_and_foreign)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    response = post_raw(client, bad_and_foreign, actor="rev-1")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    response = post_raw(client, bad_and_foreign)
    assert (response.status_code, response.json()) == (400, MALFORMED)
    response = post(client, system_id="sys-99")
    assert response.status_code == 403
    assert response.json()["error_code"] == "SYSTEM_MISMATCH"
    assert rows(owner_engine, "SELECT * FROM metric_sample") == []
    assert entry_types(owner_engine) == ["SEED"]


def test_auth_matrix_on_ingest(client: ApiClient, owner_engine: Engine) -> None:
    """Spec 1.9 Auth matrix, row by row."""
    no_key = client.request("POST", "/v1/metrics", json=metric())
    assert (no_key.status_code, no_key.json()) == (401, UNAUTHORIZED)
    wrong = client.request(
        "POST", "/v1/metrics", headers={"Authorization": "Bearer wrong"}, json=metric()
    )
    assert (wrong.status_code, wrong.json()) == (401, UNAUTHORIZED)
    reviewer = post(client, actor="rev-1")
    assert (reviewer.status_code, reviewer.json()) == (403, FORBIDDEN)
    mismatch = post(client, system_id="sys-7")
    assert mismatch.status_code == 403
    assert set(mismatch.json()) == {"error_code", "message"}
    assert mismatch.json()["error_code"] == "SYSTEM_MISMATCH"
    assert rows(owner_engine, "SELECT * FROM metric_sample") == []


# Spec 4, order of checks: 401, then 403, then body validation (400). Final spec check F1: the
# body must not be read before the key and the role are checked, even when it is not JSON.
@pytest.mark.parametrize(
    ("path", "caller", "status", "error_code"),
    [
        ("/v1/webhooks", None, 401, "MISSING_OR_INVALID_KEY"),
        ("/v1/webhooks", "wrong", 401, "MISSING_OR_INVALID_KEY"),
        ("/v1/webhooks", "sys-42", 403, "FORBIDDEN_ROLE"),
        ("/v1/webhooks", "viewer-1", 400, "MALFORMED_BODY"),
        ("/v1/webhooks", "rev-1", 400, "MALFORMED_BODY"),
        ("/v1/metrics", None, 401, "MISSING_OR_INVALID_KEY"),
        ("/v1/metrics", "viewer-1", 403, "FORBIDDEN_ROLE"),
        ("/v1/metrics", "sys-42", 400, "MALFORMED_BODY"),
        ("/v1/events/evt-none/disposition", None, 401, "MISSING_OR_INVALID_KEY"),
        ("/v1/events/evt-none/disposition", "viewer-1", 403, "FORBIDDEN_ROLE"),
    ],
)
def test_an_unparseable_body_is_checked_after_the_key_and_role(
    client: ApiClient, path: str, caller: str | None, status: int, error_code: str
) -> None:
    headers = {"Content-Type": "application/json"}
    if caller == "wrong":
        headers["Authorization"] = "Bearer not-a-real-key"
    elif caller is not None:
        headers.update(bearer(caller))
    response = client.request("POST", path, content="{bad", headers=headers)
    assert (response.status_code, response.json()["error_code"]) == (status, error_code)
