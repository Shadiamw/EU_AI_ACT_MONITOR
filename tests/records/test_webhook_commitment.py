"""D16 (Phase 5 spec 4.6, LIM-13): WEBHOOK_REGISTERED carries url_sha256, not the URL."""

import hashlib

from sqlalchemy import Engine

from app import webhooks
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.records.conftest import ledger_rows, rows
from tests.worker.conftest import Receiver


def test_webhook_registered_carries_url_sha256_not_url(
    client: ApiClient, owner_engine: Engine, receiver: Receiver
) -> None:
    response = client.request(
        "POST",
        "/v1/webhooks",
        headers=bearer("rev-1"),
        json={"url": receiver.url, "event_types": ["reminder", "flag_notification"]},
    )
    assert response.status_code == 201
    webhook_id = response.json()["webhook_id"]
    [entry] = [e for e in ledger_rows(owner_engine) if e["entry_type"] == "WEBHOOK_REGISTERED"]
    digest = hashlib.sha256(receiver.url.encode("utf-8")).hexdigest()
    assert entry["payload"] == {
        "webhook_id": webhook_id,
        "url_sha256": digest,
        "event_types": ["reminder", "flag_notification"],
    }
    assert "url" not in entry["payload"]
    assert receiver.url not in str(entry["payload"])
    # The URL stays where delivery needs it.
    [stored] = rows(owner_engine, "SELECT url FROM webhook")
    assert stored["url"] == receiver.url


def test_url_sha256_is_lowercase_hex_of_the_utf8_url() -> None:
    url = "https://example.test/é/日本?token=abc"
    assert webhooks.url_sha256(url) == hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert webhooks.url_sha256(url) == webhooks.url_sha256(url).lower()
    assert len(webhooks.url_sha256(url)) == 64
