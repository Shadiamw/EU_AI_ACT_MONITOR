"""Webhooks: URL rules, the one HTTP call the worker makes, and registration.

Registration (POST /v1/webhooks, API contract) pings the URL before any write, and stores the
webhook only if the ping gets a 2xx answer. Storing it and appending
WEBHOOK_REGISTERED share one transaction; the append is its last write.

Every webhook URL appears in GET /v1/ledger/export for any REVIEWER or VIEWER (decision 32),
and a ledger entry can never be removed, so a URL that could carry a secret is refused: no user
name or password, no query string and no fragment (integration_notes.md 4.4).
"""

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, config, ledger
from app.formats import new_id
from app.ledger.canonical import JsonValue

logger = logging.getLogger("worker")

# The notification types a webhook may subscribe to (API contract). Phase 3 sends only
# flag_notification, for flagged events and gaps alike; reminders (REQ-11) come with deadlines.
NOTIFICATION_TYPES = ("flag_notification", "reminder")
MAX_URL_LENGTH = 2048

# post(url, body, timeout_seconds) -> HTTP status, or None when there was no response.
Poster = Callable[[str, dict[str, Any], int], int | None]


class WebhookUrlError(ValueError):
    """The URL is not one the system will call."""


class VerificationFailed(Exception):
    """The verification ping did not get a 2xx answer."""


def check_url(url: str) -> None:
    """Raise WebhookUrlError unless url is an absolute http(s) URL that carries no secret."""
    if len(url) > MAX_URL_LENGTH:
        raise WebhookUrlError("too long")
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise WebhookUrlError("malformed") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookUrlError("not an absolute http or https URL")
    if parts.username is not None or parts.password is not None:
        raise WebhookUrlError("carries credentials")
    if parts.query or "?" in url:
        raise WebhookUrlError("has a query string")
    if parts.fragment or "#" in url:
        raise WebhookUrlError("has a fragment")
    if port == 0:
        raise WebhookUrlError("malformed")


def post_json(url: str, body: dict[str, Any], timeout_seconds: int) -> int | None:
    """POST body as JSON. Redirects are not followed. Any transport failure gives None."""
    try:
        response = httpx.post(url, json=body, timeout=timeout_seconds, follow_redirects=False)
    except httpx.HTTPError as exc:
        logger.info("webhook call failed: type=%s", type(exc).__name__)
        return None
    return response.status_code


def is_success(status: int | None) -> bool:
    return status is not None and 200 <= status <= 299


def register(
    session: Session,
    actor_id: str,
    url: str,
    event_types: list[str],
    post: Poster = post_json,
) -> str:
    """Ping url, then store the webhook and append WEBHOOK_REGISTERED. Returns the webhook id.

    The caller commits. Raises WebhookUrlError for a refused URL and VerificationFailed when
    the ping fails; in both cases nothing has been written.
    """
    check_url(url)
    unknown = [t for t in event_types if t not in NOTIFICATION_TYPES]
    if not event_types or unknown:
        raise ValueError("event_types must be a non-empty list of known notification types")
    event_types = list(dict.fromkeys(event_types))  # drop repeats, keep the given order
    webhook_id = new_id("wh")

    # The ping runs before this function's first write, so it never runs while the ledger
    # lock is held (integration_notes.md 2.3).
    status = post(
        url,
        {"type": "webhook_verification", "webhook_id": webhook_id},
        config.webhook_timeout_seconds(),
    )
    if not is_success(status):
        raise VerificationFailed(status)

    session.execute(
        text(
            "INSERT INTO webhook (webhook_id, url, event_types, registered_by, registered_at)"
            " VALUES (:webhook_id, :url, :event_types, :actor_id, :now)"
        ),
        {
            "webhook_id": webhook_id,
            "url": url,
            "event_types": event_types,
            "actor_id": actor_id,
            "now": clock.now(),
        },
    )
    stored_types: list[JsonValue] = list(event_types)
    ledger.append(
        session,
        "WEBHOOK_REGISTERED",
        actor_id,
        webhook_id,
        {"webhook_id": webhook_id, "url": url, "event_types": stored_types},
    )
    return webhook_id
