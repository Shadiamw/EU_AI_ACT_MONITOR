"""Part 3: POST /v1/webhooks (API contract; spec 3.6) and the webhook URL rules.

Expected values come from spec 3.6 and the shared rules (spec 4): REVIEWER or VIEWER only;
checks in the order 401, 403, body (400), then the verification ping; ping body
{"notification_type": "verification_ping", "sent_at": ...}; 201 {"webhook_id": "wh-..."}; 422
WEBHOOK_UNVERIFIED when the ping gets no 2xx; and the URL rules of decision 98 (http or https
only; outside the test environment https only and no private or loopback address).
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine

from app import webhooks
from tests.api.conftest import (
    FORBIDDEN,
    MALFORMED,
    UNAUTHORIZED,
    ApiClient,
    append_lock_is_free,
    as_actor,
    build_app,
)
from tests.worker.conftest import (
    FakeClock,
    Receiver,
    app_sessions_in_transaction,
    closed_port_url,
    entry_types,
    rows,
)

UNVERIFIED = {
    "error_code": "WEBHOOK_UNVERIFIED",
    "message": "the webhook did not answer the verification ping with 2xx",
}


@pytest.fixture
def client() -> Iterator[ApiClient]:
    with ApiClient(build_app()) as api_client:
        yield api_client


def register(client: ApiClient, body: Any, actor: str | None = "rev-1") -> Any:
    headers = {} if actor is None else as_actor(actor)
    return client.request("POST", "/v1/webhooks", json=body, headers=headers)


def nothing_written(owner_engine: Engine) -> bool:
    return entry_types(owner_engine) == [] and rows(owner_engine, "SELECT * FROM webhook") == []


def registered(owner_engine: Engine, webhook_id: str) -> tuple[Any, Any]:
    """The stored webhook row and the WEBHOOK_REGISTERED entry."""
    [stored] = rows(owner_engine, "SELECT * FROM webhook")
    [entry] = rows(
        owner_engine, "SELECT entry_type, actor_id, subject_id, payload FROM ledger_entry"
    )
    assert stored["webhook_id"] == webhook_id
    return stored, entry


@pytest.mark.parametrize("actor", ["rev-1", "viewer-1"])
def test_register_pings_then_stores_and_appends(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    actor: str,
) -> None:
    receiver.during = lambda: (
        app_sessions_in_transaction(owner_engine),
        append_lock_is_free(owner_engine),
    )
    response = register(
        client, {"url": receiver.url, "event_types": ["flag_notification", "reminder"]}, actor
    )
    assert response.status_code == 201
    assert set(response.json()) == {"webhook_id"}
    webhook_id = response.json()["webhook_id"]
    assert webhook_id.startswith("wh-")

    [ping] = receiver.calls
    assert ping.observed == (0, True)  # no transaction open, ledger lock free (spec 7)

    stored, entry = registered(owner_engine, webhook_id)
    assert entry == {
        "entry_type": "WEBHOOK_REGISTERED",
        "actor_id": actor,
        "subject_id": webhook_id,
        "payload": {
            "webhook_id": webhook_id,
            "url": receiver.url,
            "event_types": ["flag_notification", "reminder"],
        },
    }
    assert stored == {
        "webhook_id": webhook_id,
        "url": receiver.url,
        "event_types": ["flag_notification", "reminder"],
        "created_at": fake_clock.now(),
    }


def test_ping_body_is_exactly_the_spec_shape(
    client: ApiClient, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.6: POST {"notification_type": "verification_ping", "sent_at": ...}; sent_at in the
    response format YYYY-MM-DDTHH:MM:SSZ (fraction only if present). Fake clock T0 + 1.5 s."""
    fake_clock.advance(1.5)
    assert register(client, {"url": receiver.url, "event_types": ["reminder"]}).status_code == 201
    [ping] = receiver.calls
    assert ping.body == {
        "notification_type": "verification_ping",
        "sent_at": "2026-10-01T09:00:01.5Z",
    }


def test_repeated_event_types_are_stored_once(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    body = {"url": receiver.url, "event_types": ["reminder", "flag_notification", "reminder"]}
    assert register(client, body).status_code == 201
    [stored] = rows(owner_engine, "SELECT event_types FROM webhook")
    assert stored["event_types"] == ["reminder", "flag_notification"]


def test_a_long_url_and_many_event_types_are_accepted(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.6 sets no length limit and no entry limit (finding N6)."""
    url = receiver.url + "/" + "a" * 3000
    body = {"url": url, "event_types": ["flag_notification", "reminder"] * 8}
    response = register(client, body)
    assert response.status_code == 201
    stored, _ = registered(owner_engine, response.json()["webhook_id"])
    assert (stored["url"], stored["event_types"]) == (url, ["flag_notification", "reminder"])


@pytest.mark.parametrize("status", [200, 204, 299])
def test_any_2xx_ping_answer_is_accepted(
    client: ApiClient, fake_clock: FakeClock, receiver: Receiver, status: int
) -> None:
    receiver.statuses = [status]
    body = {"url": receiver.url, "event_types": ["flag_notification"]}
    assert register(client, body).status_code == 201


@pytest.mark.parametrize("status", [301, 400, 404, 500])
def test_a_non_2xx_ping_is_422_webhook_unverified_and_writes_nothing(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver, status: int
) -> None:
    receiver.statuses = [status]
    response = register(client, {"url": receiver.url, "event_types": ["flag_notification"]})
    assert (response.status_code, response.json()) == (422, UNVERIFIED)
    assert len(receiver.calls) == 1
    assert nothing_written(owner_engine)


def test_an_unreachable_url_is_422_webhook_unverified_and_writes_nothing(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    response = register(client, {"url": closed_port_url(), "event_types": ["reminder"]})
    assert (response.status_code, response.json()) == (422, UNVERIFIED)
    assert nothing_written(owner_engine)


def test_a_ping_that_times_out_is_422_after_five_seconds(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.6: a 5-second timeout; a timeout is 422 and nothing is stored."""
    timeouts: list[int] = []

    def timing_out(url: str, body: dict[str, Any], timeout_seconds: int) -> int | None:
        timeouts.append(timeout_seconds)
        return None  # what post_json returns on httpx.TimeoutException

    monkeypatch.setattr(webhooks, "post_json", timing_out)
    response = register(client, {"url": "http://198.51.100.7/hook", "event_types": ["reminder"]})
    assert (response.status_code, response.json()) == (422, UNVERIFIED)
    assert timeouts == [5]
    assert nothing_written(owner_engine)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"event_types": ["flag_notification"]},  # missing url
        {"url": "http://127.0.0.1:9/hook"},  # missing event_types
        {"url": "http://127.0.0.1:9/hook", "event_types": []},  # empty
        {"url": "http://127.0.0.1:9/hook", "event_types": ["email"]},  # outside the two values
        {"url": "http://127.0.0.1:9/hook", "event_types": ["reminder", "email"]},
        {"url": "http://127.0.0.1:9/hook", "event_types": "flag_notification"},
        {"url": 42, "event_types": ["flag_notification"]},
        {"url": "http://127.0.0.1:9/hook", "event_types": ["reminder"], "secret": "x"},
        ["http://127.0.0.1:9/hook"],
    ],
)
def test_a_malformed_body_is_400_and_nothing_is_pinged(
    client: ApiClient, owner_engine: Engine, receiver: Receiver, body: Any
) -> None:
    response = register(client, body)
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert receiver.calls == []
    assert nothing_written(owner_engine)


def test_a_body_that_is_not_json_is_400(client: ApiClient, owner_engine: Engine) -> None:
    response = client.request(
        "POST",
        "/v1/webhooks",
        content=b"{not json",
        headers={**as_actor("rev-1"), "Content-Type": "application/json"},
    )
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert nothing_written(owner_engine)


# Decision 98 (spec 3.6): any http or https URL, stored as received


@pytest.mark.parametrize(
    ("make_url", "wire_path"),
    [
        (lambda base: base + "?token=abc&x=1", "/hook?token=abc&x=1"),
        (lambda base: base.replace("http://", "http://user:pass@"), "/hook"),
        (lambda base: base + "#frag", "/hook"),  # a fragment is never sent on the wire
    ],
    ids=["query", "credentials", "fragment"],
)
def test_urls_with_query_credentials_or_fragment_are_accepted_as_received(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    make_url: Any,
    wire_path: str,
) -> None:
    url = make_url(receiver.url)
    response = register(client, {"url": url, "event_types": ["flag_notification"]})
    assert response.status_code == 201
    webhook_id = response.json()["webhook_id"]
    assert set(response.json()) == {"webhook_id"} and webhook_id.startswith("wh-")

    stored, entry = registered(owner_engine, webhook_id)
    assert stored["url"] == url  # stored as received
    assert entry["subject_id"] == webhook_id
    assert entry["payload"]["url"] == url  # the ledger carries it as received
    [ping] = receiver.calls  # the ping reached the receiver at the URL's path and query
    assert ping.path == wire_path


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "file:///etc/passwd",
        "example.com/hook",
        "/hook",
        "",
        "not a url",
        "http://",
        "https:///hook",
        "http://:80/hook",
        "http://example.com:notaport/hook",
        "http://example.com:70000/hook",
    ],
)
def test_a_non_http_url_is_400_and_nothing_is_stored(
    client: ApiClient, owner_engine: Engine, receiver: Receiver, url: str
) -> None:
    response = register(client, {"url": url, "event_types": ["flag_notification"]})
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert receiver.calls == []
    assert nothing_written(owner_engine)


def test_the_test_environment_allows_any_address(
    client: ApiClient, fake_clock: FakeClock, receiver: Receiver
) -> None:
    """Spec 3.6: test environments allow any address (here http and 127.0.0.1)."""
    assert receiver.url.startswith("http://127.0.0.1:")
    assert register(client, {"url": receiver.url, "event_types": ["reminder"]}).status_code == 201


# Outside the test environment (spec 3.6; decision 99)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """ENVIRONMENT=production. Pings are recorded, not sent; host names resolve through a fixed
    table, so no test depends on real DNS."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    pinged: list[str] = []

    def record_ping(url: str, body: dict[str, Any], timeout_seconds: int) -> int | None:
        pinged.append(url)
        return 200

    table = {
        "hooks.example.com": ["93.184.215.14"],
        "dual.example.com": ["93.184.215.14", "10.1.2.3"],
        "internal.example.com": ["10.1.2.3"],
        "metadata.example.com": ["169.254.169.254"],
        "v6loop.example.com": ["::1"],
    }
    monkeypatch.setattr(webhooks, "post_json", record_ping)
    monkeypatch.setattr(webhooks, "resolve_host", lambda host, port: table.get(host, []))
    return pinged


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",  # http outside test
        "http://hooks.example.com/x",
        "https://127.0.0.1",  # IPv4 loopback
        "https://127.8.9.10/x",
        "https://[::1]",  # IPv6 loopback
        "https://[::ffff:127.0.0.1]/x",  # IPv4-mapped loopback
        "https://10.0.0.1",  # RFC 1918
        "https://172.16.5.4",
        "https://192.168.1.1",
        "https://169.254.169.254",  # link-local (cloud metadata)
        "https://[fe80::1]/x",  # IPv6 link-local
        "https://[fd00::1]/x",  # IPv6 unique local
        "https://0.0.0.0/x",
        "https://localhost",
        "https://LOCALHOST:8443/x",
        "https://api.localhost/x",
        "https://internal.example.com/x",  # resolves to RFC 1918
        "https://dual.example.com/x",  # one public and one private address
        "https://metadata.example.com/x",  # resolves to link-local
        "https://v6loop.example.com/x",  # resolves to ::1
    ],
)
def test_outside_test_refused_urls_are_400_and_never_pinged(
    client: ApiClient, owner_engine: Engine, production: list[str], url: str
) -> None:
    response = register(client, {"url": url, "event_types": ["flag_notification"]})
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert production == []  # no ping sent
    assert nothing_written(owner_engine)


def test_outside_test_a_hostname_resolving_to_a_private_address_is_refused(
    client: ApiClient, owner_engine: Engine, production: list[str]
) -> None:
    response = register(
        client, {"url": "https://internal.example.com/hook", "event_types": ["reminder"]}
    )
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert production == []
    assert nothing_written(owner_engine)


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com/receive?key=1#part",
        "https://93.184.215.14/hook",
        "https://user:pw@hooks.example.com:8443/hook",
    ],
)
def test_outside_test_a_public_https_url_is_accepted(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, production: list[str], url: str
) -> None:
    response = register(client, {"url": url, "event_types": ["flag_notification"]})
    assert response.status_code == 201
    assert production == [url]
    stored, entry = registered(owner_engine, response.json()["webhook_id"])
    assert stored["url"] == entry["payload"]["url"] == url


def test_outside_test_an_unresolvable_host_goes_to_the_ping(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, production: list[str]
) -> None:
    """Decision 99: a name that does not resolve is not known to be private, so the ping
    decides (here the recorded ping answers 200)."""
    response = register(
        client, {"url": "https://nowhere.example.net/x", "event_types": ["reminder"]}
    )
    assert response.status_code == 201
    assert production == ["https://nowhere.example.net/x"]


# Auth and order of checks (spec 4)


def test_no_principal_is_401_and_a_system_key_is_403(
    client: ApiClient, owner_engine: Engine, receiver: Receiver
) -> None:
    body = {"url": receiver.url, "event_types": ["flag_notification"]}
    response = register(client, body, actor=None)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    response = register(client, body, actor="sys-42")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert receiver.calls == []
    assert nothing_written(owner_engine)


@pytest.mark.parametrize(
    ("actor", "expected"),
    [(None, (401, UNAUTHORIZED)), ("sys-42", (403, FORBIDDEN)), ("viewer-1", (400, MALFORMED))],
)
def test_auth_is_checked_before_the_body(
    client: ApiClient, owner_engine: Engine, actor: str | None, expected: tuple[int, Any]
) -> None:
    """Spec 4: 401, then 403, then body validation (400). A bad body with a bad key gets the
    key's error; the ping comes last (a malformed body is never pinged)."""
    response = register(client, {"url": "ftp://example.com", "event_types": []}, actor=actor)
    assert (response.status_code, response.json()) == expected
    assert nothing_written(owner_engine)


def test_wrong_method_is_404(client: ApiClient) -> None:
    response = client.get("/v1/webhooks", headers=as_actor("rev-1"))
    assert response.status_code == 404


def test_the_real_app_mounts_the_webhook_route(receiver: Receiver, fake_clock: FakeClock) -> None:
    from app.main import create_app

    with ApiClient(create_app()) as real_client:
        # The stub resolver in app/auth.py is always rev-1.
        response = real_client.request(
            "POST", "/v1/webhooks", json={"url": receiver.url, "event_types": ["reminder"]}
        )
    assert response.status_code == 201


# The URL rules on their own


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/hook",
        "https://receiver.example/hooks/abc",
        "http://[::1]:8000/",
        "HTTPS://receiver.example",
        "http://user:pw@receiver.example/hook?token=1#x",
        "http://receiver.example/hook?",
        "http://receiver.example/" + "a" * 5000,
    ],
)
def test_check_url_accepts_any_http_url_in_test(url: str) -> None:
    webhooks.check_url(url, any_address=True)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "receiver.example/hook",
        "/hook",
        "ftp://receiver.example/hook",
        "file:///etc/passwd",
        "http://",
        "http://:80/hook",
        "http://receiver.example:notaport/hook",
        "http://receiver.example:70000/hook",
        "http://receiver.example:0/hook",
    ],
)
def test_check_url_refuses_what_is_not_an_http_url(url: str) -> None:
    with pytest.raises(webhooks.WebhookUrlError):
        webhooks.check_url(url, any_address=True)


def test_check_url_outside_test_uses_the_resolver() -> None:
    def resolve(host: str, port: int) -> list[str]:
        return {"good.example": ["8.8.8.8"], "bad.example": ["192.168.0.9"]}.get(host, [])

    webhooks.check_url("https://good.example/x", any_address=False, resolve=resolve)
    with pytest.raises(webhooks.WebhookUrlError):
        webhooks.check_url("https://bad.example/x", any_address=False, resolve=resolve)
    with pytest.raises(webhooks.WebhookUrlError):
        webhooks.check_url("http://good.example/x", any_address=False, resolve=resolve)
