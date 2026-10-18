"""Webhooks: URL rules, the one HTTP call the worker makes, and registration (spec 3.6).

The only URL rules are the ones spec 3.6 states (decision 98, superseding decision 87):

- the URL must be an http or https URL with a host; anything else is refused;
- outside the test environment (ENVIRONMENT is not "test"), only https is allowed, and a host
  that is, or resolves to, a private, loopback, link-local or otherwise non-public address is
  refused (decision 99). The test environment allows any address.

Within those rules any URL is accepted and stored exactly as received, including one with a
query string, a user name and password, or a fragment. Every webhook URL appears in
GET /v1/ledger/export for any REVIEWER or VIEWER (decision 32), so avoiding secrets in webhook
URLs is recommended, but it is not enforced (integration_notes.md 4.4).

Registration pings the URL before any write, and stores the webhook only if the ping gets a 2xx
answer. Storing it and appending WEBHOOK_REGISTERED share one transaction; the append is its
last write.
"""

import ipaddress
import logging
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, config, ledger
from app.formats import api_time, new_id
from app.ledger.canonical import JsonValue

logger = logging.getLogger("worker")

# Logs never carry webhook URLs (decision 106). httpx logs every request's full URL, user name,
# password, query and fragment included, at INFO, and httpcore traces connections at DEBUG. Both
# are held at WARNING here, where the client is used, so the worker's deliveries and the web
# process's verification ping are both covered whatever LOG_LEVEL says.
for _client_logger in ("httpx", "httpcore"):
    logging.getLogger(_client_logger).setLevel(logging.WARNING)

# The notification types a webhook may subscribe to (API contract). Phase 3 sends only
# flag_notification, for flagged events and gaps alike; reminders (REQ-11) come with deadlines.
NOTIFICATION_TYPES = ("flag_notification", "reminder")
TIMEOUT_SECONDS = 5  # spec 3.5 step 3 and 3.6: deliveries and the verification ping

# post(url, body, timeout_seconds) -> HTTP status, or None when there was no response.
Poster = Callable[[str, dict[str, Any], int], int | None]
# resolve(host, port) -> the IP addresses the host resolves to (empty if it does not resolve).
Resolver = Callable[[str, int], list[str]]


class WebhookUrlError(ValueError):
    """The URL is not one the system will call."""


class VerificationFailed(Exception):
    """The verification ping did not get a 2xx answer."""


def resolve_host(host: str, port: int) -> list[str]:
    """The addresses `host` resolves to, or [] when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _is_private_host(host: str, port: int, resolve: Resolver) -> bool:
    """True when the host is localhost, or is or resolves to a non-public address."""
    name = host.rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return not _is_public_address(name)
    except ValueError:
        pass  # not an IP literal: a host name
    return any(not _is_public_address(address) for address in resolve(name, port))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False
    return True


def _client_can_use(url: str, hostname: str) -> bool:
    """True when the HTTP client can call url at the host it names (decision 107).

    The URL is parsed by httpx.URL, the parser httpx.post uses, so the check and the call cannot
    disagree: whatever it refuses (a control character, an invalid IP address, an over-long URL,
    a malformed IDNA label) is refused here. Two more checks use the client's own machinery
    rather than a list of characters: httpx must read the same host as urlsplit (it
    percent-encodes characters that cannot be in a host, such as a space, instead of refusing
    them), and a host name must pass the `idna` codec that the socket layer applies when it
    connects (an empty or over-long label fails there)."""
    try:
        parsed = httpx.URL(url)
        if parsed.host.lower() != hostname:  # urlsplit lower-cases the host; httpx keeps IPv6 case
            return False
        if not _is_ip_literal(hostname):
            parsed.raw_host.decode("ascii").encode("idna")
    except Exception:  # any parse or encoding error: the client could not call this URL
        return False
    return True


def check_url(url: str, *, any_address: bool, resolve: Resolver | None = None) -> None:
    """Raise WebhookUrlError unless url is an http or https URL with a host that the HTTP
    client can use (spec 3.6; decision 107). With any_address False (outside the test
    environment) also require https and a public host."""
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise WebhookUrlError("not a URL") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname or port == 0:
        raise WebhookUrlError("not an http or https URL")
    if not _client_can_use(url, parts.hostname):
        raise WebhookUrlError("not a URL the HTTP client can use")
    if any_address:
        return
    if scheme != "https":
        raise WebhookUrlError("only https outside the test environment")
    lookup = resolve if resolve is not None else resolve_host
    if _is_private_host(parts.hostname, port or 443, lookup):
        raise WebhookUrlError("a private or loopback address outside the test environment")


def post_json(url: str, body: dict[str, Any], timeout_seconds: int) -> int | None:
    """POST body as JSON. Redirects are not followed. Any transport failure gives None, and so
    does a URL the client refuses after all (decision 107: check_url should have caught it).
    Only the exception's type name is logged: its text can quote the URL (decision 106)."""
    try:
        response = httpx.post(url, json=body, timeout=timeout_seconds, follow_redirects=False)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
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
    post: Poster | None = None,
    resolve: Resolver | None = None,
) -> str:
    """Check url, ping it, then store the webhook and append WEBHOOK_REGISTERED. Returns the
    webhook id.

    The caller commits. Raises WebhookUrlError for a refused URL (no ping is sent) and
    VerificationFailed when the ping fails; in both cases nothing has been written.
    """
    check_url(url, any_address=config.is_test_environment(), resolve=resolve)
    unknown = [t for t in event_types if t not in NOTIFICATION_TYPES]
    if not event_types or unknown:
        raise ValueError("event_types must be a non-empty list of known notification types")
    event_types = list(dict.fromkeys(event_types))  # drop repeats, keep the given order
    webhook_id = new_id("wh")

    # The ping runs before this function's first write, so no transaction is open and no lock
    # is held during it (spec 3.6 and 7).
    send = post if post is not None else post_json
    ping = {"notification_type": "verification_ping", "sent_at": api_time(clock.now())}
    if not is_success(send(url, ping, TIMEOUT_SECONDS)):
        raise VerificationFailed()

    session.execute(
        text(
            "INSERT INTO webhook (webhook_id, url, event_types, created_at)"
            " VALUES (:webhook_id, :url, :event_types, :now)"
        ),
        {"webhook_id": webhook_id, "url": url, "event_types": event_types, "now": clock.now()},
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
