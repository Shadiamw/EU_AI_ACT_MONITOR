"""Notifications (REQ-04, and REQ-02 for gaps): enqueue, and the delivery pass.

enqueue runs inside the caller's transaction (Part 1's ingest for a flagged event, the gap
sweeper for a gap). It records the notification, creates one delivery row for each webhook
subscribed to its type, and appends NOTIFICATION_REQUESTED as its last write.

The delivery pass never calls a webhook while a transaction is open (spec, 'Transactions and
lock order (Pinned)'). For each due delivery it:

1. claims the row in a short transaction (FOR UPDATE SKIP LOCKED, then status IN_FLIGHT with
   a lease) and commits;
2. makes the HTTP call with no transaction open;
3. in a new, short transaction, records the result. A 2xx answer appends
   NOTIFICATION_DELIVERED; the last failed attempt appends NOTIFICATION_FAILED; an earlier
   failure schedules a retry and appends nothing.

A claim whose result was never recorded (a crash, or a lock timeout while recording) becomes
due again when its lease runs out, so delivery is at least once: a receiver can see the same
notification_id twice.

Delivered means the webhook answered 2xx within the timeout. latency_seconds counts from the
moment the notification was requested, which is the moment the event was detected, to the
moment the answer arrived, on the simulated clock.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import clock, config, ledger, webhooks
from app.formats import api_time, new_id, whole_seconds
from app.ledger.canonical import LedgerError

logger = logging.getLogger("worker")

FLAG_NOTIFICATION = "flag_notification"

# Retry n (after failed attempt n) waits RETRY_BASE_SECONDS * 2**(n-1), at most
# RETRY_MAX_SECONDS. With the defaults, five attempts start at 0, 30, 90, 210 and 450 seconds,
# all inside the 900-second bound.
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 240
CLAIM_BATCH_SIZE = 50


def enqueue(
    session: Session, event_id: str, actor_id: str, notification_type: str = FLAG_NOTIFICATION
) -> str:
    """Request a notification for an event, in the caller's transaction. Returns its id."""
    if notification_type not in webhooks.NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type {notification_type!r}")
    notification_id = new_id("ntf")
    now = clock.now()
    session.execute(
        text(
            "INSERT INTO notification (notification_id, event_id, notification_type,"
            " requested_at) VALUES (:notification_id, :event_id, :type, :now)"
        ),
        {
            "notification_id": notification_id,
            "event_id": event_id,
            "type": notification_type,
            "now": now,
        },
    )
    session.execute(
        text(
            "INSERT INTO notification_delivery"
            " (notification_id, webhook_id, status, attempts, next_attempt_at)"
            " SELECT :notification_id, webhook_id, 'PENDING', 0, :now FROM webhook"
            " WHERE :type = ANY(event_types)"
        ),
        {"notification_id": notification_id, "type": notification_type, "now": now},
    )
    ledger.append(
        session,
        "NOTIFICATION_REQUESTED",
        actor_id,
        event_id,
        {
            "notification_id": notification_id,
            "event_id": event_id,
            "notification_type": notification_type,
        },
    )
    return notification_id


@dataclass(frozen=True)
class Claim:
    notification_id: str
    webhook_id: str
    attempt: int
    url: str
    event_id: str
    notification_type: str
    requested_at: datetime


def retry_delay_seconds(failed_attempt: int) -> int:
    return int(min(RETRY_BASE_SECONDS * 2 ** (failed_attempt - 1), RETRY_MAX_SECONDS))


def lease_seconds() -> int:
    """How long a claim stays IN_FLIGHT before another pass may take it again."""
    return config.webhook_timeout_seconds() + 60


def deliver_due(engine: Engine, post: webhooks.Poster = webhooks.post_json) -> int:
    """Run one delivery pass. Returns the number of deliveries attempted."""
    try:
        claims = _claim(engine)
    except SQLAlchemyError as exc:
        logger.warning("delivery claim failed: type=%s", type(exc).__name__)
        return 0
    for claim in claims:
        status = post(claim.url, _body(claim), config.webhook_timeout_seconds())
        try:
            _record(engine, claim, status)
        except (SQLAlchemyError, LedgerError) as exc:
            # Rolled back: the claim stays IN_FLIGHT and is retried when its lease runs out.
            logger.warning(
                "delivery result not recorded: notification_id=%s webhook_id=%s type=%s",
                claim.notification_id,
                claim.webhook_id,
                type(exc).__name__,
            )
    return len(claims)


def _claim(engine: Engine) -> list[Claim]:
    with Session(engine) as session:
        now = clock.now()
        rows = session.execute(
            text(
                "SELECT d.notification_id, d.webhook_id, d.attempts, w.url, n.event_id,"
                " n.notification_type, n.requested_at"
                " FROM notification_delivery d"
                " JOIN notification n ON n.notification_id = d.notification_id"
                " JOIN webhook w ON w.webhook_id = d.webhook_id"
                " WHERE (d.status = 'PENDING' AND d.next_attempt_at <= :now)"
                " OR (d.status = 'IN_FLIGHT' AND d.lease_expires_at <= :now)"
                " ORDER BY d.next_attempt_at, d.notification_id, d.webhook_id"
                " LIMIT :limit FOR UPDATE OF d SKIP LOCKED"
            ),
            {"now": now, "limit": CLAIM_BATCH_SIZE},
        ).all()
        claims = []
        for row in rows:
            attempt = row.attempts + 1
            session.execute(
                text(
                    "UPDATE notification_delivery SET status = 'IN_FLIGHT', attempts = :attempt,"
                    " lease_expires_at = :lease_expires_at"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {
                    "attempt": attempt,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds()),
                    "notification_id": row.notification_id,
                    "webhook_id": row.webhook_id,
                },
            )
            claims.append(
                Claim(
                    notification_id=row.notification_id,
                    webhook_id=row.webhook_id,
                    attempt=attempt,
                    url=row.url,
                    event_id=row.event_id,
                    notification_type=row.notification_type,
                    requested_at=row.requested_at,
                )
            )
        session.commit()
    return claims


def _body(claim: Claim) -> dict[str, Any]:
    """What the webhook receives. The receiver fetches the event itself if it needs more."""
    return {
        "type": claim.notification_type,
        "notification_id": claim.notification_id,
        "webhook_id": claim.webhook_id,
        "event_id": claim.event_id,
        "requested_at": api_time(claim.requested_at),
        "attempt": claim.attempt,
    }


def _record(engine: Engine, claim: Claim, status: int | None) -> None:
    with Session(engine) as session:
        current = session.execute(
            text(
                "SELECT status, attempts FROM notification_delivery"
                " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                " FOR UPDATE"
            ),
            {"notification_id": claim.notification_id, "webhook_id": claim.webhook_id},
        ).one()
        if current.status != "IN_FLIGHT" or current.attempts != claim.attempt:
            # Another pass took this delivery over after our lease ran out; its result wins.
            session.rollback()
            return
        now = clock.now()
        keys = {"notification_id": claim.notification_id, "webhook_id": claim.webhook_id}
        if webhooks.is_success(status):
            latency = whole_seconds(claim.requested_at, now)
            session.execute(
                text(
                    "UPDATE notification_delivery SET status = 'DELIVERED',"
                    " lease_expires_at = NULL, last_status = :status, delivered_at = :now,"
                    " latency_seconds = :latency"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {**keys, "status": status, "now": now, "latency": latency},
            )
            ledger.append(
                session,
                "NOTIFICATION_DELIVERED",
                "system",
                claim.event_id,
                {
                    **keys,
                    "attempt": claim.attempt,
                    "latency_seconds": latency,
                    "within_bound": latency <= config.notify_max_delay_seconds(),
                },
            )
        elif claim.attempt >= config.delivery_max_attempts():
            session.execute(
                text(
                    "UPDATE notification_delivery SET status = 'FAILED',"
                    " lease_expires_at = NULL, last_status = :status"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {**keys, "status": status},
            )
            ledger.append(
                session,
                "NOTIFICATION_FAILED",
                "system",
                claim.event_id,
                {**keys, "attempts": claim.attempt, "last_status": status},
            )
        else:
            session.execute(
                text(
                    "UPDATE notification_delivery SET status = 'PENDING',"
                    " lease_expires_at = NULL, last_status = :status, next_attempt_at = :next"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {
                    **keys,
                    "status": status,
                    "next": now + timedelta(seconds=retry_delay_seconds(claim.attempt)),
                },
            )
        session.commit()
