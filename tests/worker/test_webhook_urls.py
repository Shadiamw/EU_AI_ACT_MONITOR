"""URLs the HTTP client cannot use are 400, never 500 (spec 3.6; decision 107).

Spec 3.6: "url must be an http or https URL ... Anything else: 400 MALFORMED_BODY", and a ping
that gets no 2xx answer is 422 WEBHOOK_UNVERIFIED with nothing stored. Before decision 107 a URL
that urlsplit accepted but httpx refused (a control character, for example) reached the ping,
httpx raised InvalidURL, and the request ended as 500 INTERNAL_ERROR.

Expected values, written before the run: each refused URL gives 400 with the contract's
MALFORMED_BODY body, no ping, no webhook row, no ledger entry, and no log record holding the URL
(decision 106); the decision-98 forms still give 201; if httpx still refuses a URL at ping time,
the result is 422 WEBHOOK_UNVERIFIED with nothing stored.
"""

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from app import webhooks
from tests.api.conftest import MALFORMED, ApiClient, as_actor, build_app
from tests.worker.conftest import FakeClock, Receiver, entry_types, rows

UNVERIFIED = {
    "error_code": "WEBHOOK_UNVERIFIED",
    "message": "the webhook did not answer the verification ping with 2xx",
}


@pytest.fixture
def client() -> Iterator[ApiClient]:
    with ApiClient(build_app()) as api_client:
        yield api_client


@pytest.fixture
def pings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ping registration tries to send, recorded instead of sent."""
    sent: list[str] = []

    def record(url: str, body: dict[str, Any], timeout_seconds: int) -> int | None:
        sent.append(url)
        return 200

    monkeypatch.setattr(webhooks, "post_json", record)
    return sent


def register(client: ApiClient, url: str) -> Any:
    return client.request(
        "POST",
        "/v1/webhooks",
        json={"url": url, "event_types": ["flag_notification"]},
        headers=as_actor("rev-1"),
    )


def nothing_written(owner_engine: Engine) -> bool:
    return entry_types(owner_engine) == [] and rows(owner_engine, "SELECT * FROM webhook") == []


def logs_holding(caplog: pytest.LogCaptureFixture, *parts: str) -> list[str]:
    found = []
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        if any(part in text for part in parts):
            found.append(f"{record.name}: {text}")
    return found


# (url, a piece of it that must not appear in any log record)
CLIENT_REFUSED = [
    pytest.param("http://example.com/\n", "example.com", id="newline"),
    pytest.param("http://example.com/\t", "example.com", id="tab"),
    pytest.param("http://example.com/\u0000", "example.com", id="nul"),
    pytest.param("http://example.com/\u007f", "example.com", id="del"),
    pytest.param("http://exa mple.com/", "mple.com", id="space-in-host"),
    pytest.param("http://[::1", "[::1", id="unclosed-ipv6"),
    pytest.param("http://example.com:99999/", "example.com", id="port-99999"),
    # Found while working (decision 107): each is refused by httpx or by the idna codec.
    pytest.param("http://999.1.1.1/", "999.1.1.1", id="invalid-ipv4"),
    pytest.param("http://xn--.com/", "xn--", id="bad-idna-label"),
    pytest.param("http://a..b/", "a..b", id="empty-label"),
    pytest.param("http://" + "a" * 64 + ".com/", "a" * 64, id="64-character-label"),
    pytest.param("http://ex<ample.com/", "ample.com", id="angle-bracket-in-host"),
    pytest.param("http://example.com/" + "a" * 70000, "example.com", id="over-65536-characters"),
]


@pytest.mark.parametrize(("url", "piece"), CLIENT_REFUSED)
def test_a_url_the_client_cannot_use_is_400_and_nothing_happens(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    pings: list[str],
    caplog: pytest.LogCaptureFixture,
    url: str,
    piece: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    response = register(client, url)
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert pings == []
    assert nothing_written(owner_engine)
    assert logs_holding(caplog, url, piece) == []


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param("?token=abc", id="query"),
        pytest.param("#frag", id="fragment"),
        pytest.param("/" + "a" * 3000, id="long"),
        pytest.param("?", id="empty-query"),
    ],
)
def test_the_decision_98_forms_are_still_accepted(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver, suffix: str
) -> None:
    url = receiver.url + suffix
    response = register(client, url)
    assert response.status_code == 201
    assert rows(owner_engine, "SELECT url FROM webhook") == [{"url": url}]


def test_a_url_with_credentials_is_still_accepted(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, receiver: Receiver
) -> None:
    url = receiver.url.replace("http://", "http://user:pw@") + "?token=1#x"
    response = register(client, url)
    assert response.status_code == 201
    assert rows(owner_engine, "SELECT url FROM webhook") == [{"url": url}]
    assert len(receiver.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://[FE80::1]/",
        "http://[::FFFF:127.0.0.1]/",
        "http://EXAMPLE.COM/",
        "http://exämple.com/",
        "http://example.com./",
        "http://[fe80::1%25eth0]/",
        "http://" + "a" * 63 + ".com/",
    ],
)
def test_check_url_accepts_hosts_the_client_can_use(url: str) -> None:
    """The host comparison ignores case (httpx keeps an IPv6 literal's case, urlsplit does not),
    and a 63-character label is the longest the idna codec allows."""
    webhooks.check_url(url, any_address=True)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx.InvalidURL("refused"), id="InvalidURL"),
        pytest.param(UnicodeError("label too long"), id="UnicodeError"),
    ],
)
def test_a_url_the_client_still_refuses_at_ping_time_is_422_and_nothing_is_stored(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """Defence in depth (decision 107): if check_url lets through a URL that httpx refuses,
    post_json treats it as a failed ping, never a 500, and logs only the type name."""
    caplog.set_level(logging.INFO)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(httpx, "post", refuse)
    response = register(client, receiver.url)
    assert (response.status_code, response.json()) == (422, UNVERIFIED)
    assert receiver.calls == []
    assert nothing_written(owner_engine)
    messages = [r.getMessage() for r in caplog.records]
    assert f"webhook call failed: type={type(error).__name__}" in messages
    assert logs_holding(caplog, receiver.url, "127.0.0.1") == []
