"""Logs never carry webhook URLs (decision 106).

A webhook URL may hold a user name and password, a token in its query string, or a fragment
(decision 98). With LOG_LEVEL=INFO, neither a delivery nor a registration may write any part of
it to a log record, and the spec 3.8 attempt line (notification id, webhook id, status,
duration) must still be written.
"""

import logging
import re

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import config, notifications
from tests.api.conftest import ApiClient, as_actor, build_app
from tests.worker.conftest import FakeClock, Receiver, add_event, add_webhook, entry_types

SECRETS = ("pw", "token=secret", "#frag")


def secret_url(receiver: Receiver) -> str:
    port = receiver.url.split(":")[2].split("/")[0]
    return f"http://user:pw@127.0.0.1:{port}/hook?token=secret#frag"


def capture_at_configured_level(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    caplog.set_level(config.log_level())


def leaked(caplog: pytest.LogCaptureFixture, url: str) -> list[str]:
    """Every captured record (message and any exception text) that holds part of the URL."""
    found = []
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        if url in text or any(part in text for part in SECRETS):
            found.append(f"{record.name}: {text}")
    return found


def test_a_delivery_logs_the_attempt_and_no_part_of_the_url(
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_at_configured_level(caplog, monkeypatch)
    url = secret_url(receiver)
    add_webhook(owner_engine, "wh-1", url, ["flag_notification"])
    add_event(owner_engine, "evt-1")
    with Session(app_engine) as session:
        notification_id = notifications.enqueue(session, "evt-1")
        session.commit()

    assert notifications.deliver_due(app_engine) == 1

    assert len(receiver.calls) == 1  # the secret URL was really called
    assert leaked(caplog, url) == []
    attempt = re.compile(
        rf"^delivery attempt: notification_id={notification_id} webhook_id=wh-1 attempt=1"
        r" status=200 duration_ms=\d+$"
    )
    assert [r.getMessage() for r in caplog.records if attempt.match(r.getMessage())] != []


def test_a_registration_logs_no_part_of_the_url(
    owner_engine: Engine,
    fake_clock: FakeClock,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_at_configured_level(caplog, monkeypatch)
    url = secret_url(receiver)
    with ApiClient(build_app()) as client:
        response = client.request(
            "POST",
            "/v1/webhooks",
            json={"url": url, "event_types": ["flag_notification"]},
            headers=as_actor("rev-1"),
        )

    assert response.status_code == 201
    assert len(receiver.calls) == 1  # the verification ping reached the secret URL
    assert entry_types(owner_engine) == ["WEBHOOK_REGISTERED"]
    assert leaked(caplog, url) == []


def test_the_http_client_loggers_are_held_at_warning() -> None:
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
