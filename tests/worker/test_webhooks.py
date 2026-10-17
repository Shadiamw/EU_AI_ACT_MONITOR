"""Part 3: POST /v1/webhooks (API contract) and the webhook URL rules."""

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

VERIFICATION_FAILED = {
    "error_code": "WEBHOOK_VERIFICATION_FAILED",
    "message": "the webhook did not answer the ping with 2xx",
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
    assert ping.body == {"type": "webhook_verification", "webhook_id": webhook_id}
    assert ping.observed == (0, True)  # no transaction open, ledger lock free

    entries = rows(
        owner_engine, "SELECT entry_type, actor_id, subject_id, payload FROM ledger_entry"
    )
    assert entries == [
        {
            "entry_type": "WEBHOOK_REGISTERED",
            "actor_id": actor,
            "subject_id": webhook_id,
            "payload": {
                "webhook_id": webhook_id,
                "url": receiver.url,
                "event_types": ["flag_notification", "reminder"],
            },
        }
    ]
    [stored] = rows(owner_engine, "SELECT * FROM webhook")
    assert stored == {
        "webhook_id": webhook_id,
        "url": receiver.url,
        "event_types": ["flag_notification", "reminder"],
        "registered_by": actor,
        "registered_at": fake_clock.now(),
    }


def test_repeated_event_types_are_stored_once(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    body = {"url": receiver.url, "event_types": ["reminder", "flag_notification", "reminder"]}
    assert register(client, body).status_code == 201
    [stored] = rows(owner_engine, "SELECT event_types FROM webhook")
    assert stored["event_types"] == ["reminder", "flag_notification"]


@pytest.mark.parametrize("status", [204, 299])
def test_any_2xx_ping_answer_is_accepted(
    client: ApiClient, fake_clock: FakeClock, receiver: Receiver, status: int
) -> None:
    receiver.statuses = [status]
    body = {"url": receiver.url, "event_types": ["flag_notification"]}
    assert register(client, body).status_code == 201


@pytest.mark.parametrize("status", [301, 400, 404, 500])
def test_a_non_2xx_ping_is_422_and_writes_nothing(
    client: ApiClient, owner_engine: Engine, receiver: Receiver, status: int
) -> None:
    receiver.statuses = [status]
    response = register(client, {"url": receiver.url, "event_types": ["flag_notification"]})
    assert (response.status_code, response.json()) == (422, VERIFICATION_FAILED)
    assert len(receiver.calls) == 1
    assert nothing_written(owner_engine)


def test_an_unreachable_url_is_422_and_writes_nothing(
    client: ApiClient, owner_engine: Engine
) -> None:
    response = register(client, {"url": closed_port_url(), "event_types": ["reminder"]})
    assert (response.status_code, response.json()) == (422, VERIFICATION_FAILED)
    assert nothing_written(owner_engine)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": "http://127.0.0.1:9/hook"},
        {"event_types": ["flag_notification"]},
        {"url": "http://127.0.0.1:9/hook", "event_types": []},
        {"url": "http://127.0.0.1:9/hook", "event_types": ["email"]},
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


@pytest.mark.parametrize(
    "path_suffix",
    ["?token=abc", "?", "#frag", ""],
)
def test_a_url_that_could_carry_a_secret_is_400_and_never_called(
    client: ApiClient, owner_engine: Engine, receiver: Receiver, path_suffix: str
) -> None:
    url = receiver.url + path_suffix
    if path_suffix == "":
        url = url.replace("http://", "http://user:pass@")
    response = register(client, {"url": url, "event_types": ["flag_notification"]})
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert receiver.calls == []
    assert nothing_written(owner_engine)


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


def test_wrong_method_is_404(client: ApiClient) -> None:
    response = client.get("/v1/webhooks", headers=as_actor("rev-1"))
    assert response.status_code == 404


def test_the_real_app_mounts_the_webhook_route(receiver: Receiver, fake_clock: FakeClock) -> None:
    from app.main import create_app

    app = create_app()
    from app.auth import resolve_principal
    from tests.api.conftest import header_principal

    app.dependency_overrides[resolve_principal] = header_principal
    with ApiClient(app) as real_client:
        response = real_client.request(
            "POST",
            "/v1/webhooks",
            headers=as_actor("rev-1"),
            json={"url": receiver.url, "event_types": ["reminder"]},
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
    ],
)
def test_check_url_accepts(url: str) -> None:
    webhooks.check_url(url)


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
        "http://user@receiver.example/hook",
        "http://user:pw@receiver.example/hook",
        "http://receiver.example/hook?token=1",
        "http://receiver.example/hook?",
        "http://receiver.example/hook#x",
        "http://receiver.example:notaport/hook",
        "http://receiver.example:70000/hook",
        "http://receiver.example:0/hook",
        "http://receiver.example/" + "a" * 2048,
    ],
)
def test_check_url_refuses(url: str) -> None:
    with pytest.raises(webhooks.WebhookUrlError):
        webhooks.check_url(url)
