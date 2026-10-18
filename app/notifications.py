"""Notifications (REQ-04, and REQ-02 for gaps): enqueue, and the delivery pass (spec 3.5).

enqueue runs inside the caller's transaction (Part 1's ingest for a flagged event, the gap
sweeper for a gap). It records the notification and appends NOTIFICATION_REQUESTED as its last
write, so an event without its notification request, or the reverse, cannot exist.

The delivery pass (deliver_due) never calls a webhook while a transaction is open (spec 7):

1. Route: each notification with no delivery rows yet gets one PENDING row per webhook whose
   event_types include its type. With no such webhook it stays unrouted and is checked again on
   the next pass, so the first webhook registered later receives it. A routed notification is
   never sent to a webhook registered after routing.
2. Claim: due PENDING rows (FOR UPDATE SKIP LOCKED) get a lease (lease_until) and an attempt
   number, and the claim commits before any network call.
3. Send: an HTTP POST with a 5-second timeout and no transaction open.
4. Record, in a new short transaction: any 2xx marks DELIVERED and appends
   NOTIFICATION_DELIVERED. A failure schedules a retry 10, 30, 60, 120 or 300 seconds later;
   the sixth failed attempt marks FAILED and appends NOTIFICATION_FAILED.

A claim whose result was never recorded (a crash, or a lock timeout while recording) is claimed
again when its lease runs out, so delivery is at least once: a receiver can see the same
notification_id twice.

latency_seconds is the delivery time minus the event's received_at, in whole seconds, on the
simulated clock. within_bound is latency_seconds <= NOTIFY_MAX_DELAY_SECONDS. A late delivery is
logged as a warning and counted; the bound never delays sending.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, ledger, webhooks
from app.formats import api_time, new_id, whole_seconds

logger = logging.getLogger("worker")

FLAG_NOTIFICATION = "flag_notification"
WORKER_ACTOR = "system"  # background work (spec, 'Ledger entry format'; decision 93)

# Spec 3.5 step 5: retry delays after failed attempts 1 to 5; attempt 6 is the last.
RETRY_DELAYS_SECONDS = (10, 30, 60, 120, 300)
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS) + 1
LEASE_SECONDS = webhooks.TIMEOUT_SECONDS + 60
CLAIM_BATCH_SIZE = 50

_late_deliveries = 0


def late_delivery_count() -> int:
    """Late deliveries (within_bound false) recorded by this process (spec 3.5)."""
    return _late_deliveries


def enqueue(session: Session, event_id: str, notification_type: str = FLAG_NOTIFICATION) -> str:
    """Request a notification for an event, in the caller's transaction (spec 6). Appends
    NOTIFICATION_REQUESTED with actor `system` and returns the notification id. Never commits.
    An unknown notification type raises ValueError before anything is written."""
    if notification_type not in webhooks.NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type {notification_type!r}")
    notification_id = new_id("ntf")
    session.execute(
        text(
            "INSERT INTO notification (notification_id, event_id, notification_type,"
            " created_at) VALUES (:notification_id, :event_id, :type, :now)"
        ),
        {
            "notification_id": notification_id,
            "event_id": event_id,
            "type": notification_type,
            "now": clock.now(),
        },
    )
    ledger.append(
        session,
        "NOTIFICATION_REQUESTED",
        WORKER_ACTOR,
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
    lease_until: datetime  # identifies this claim: a later claim sets a new lease
    url: str
    notification_type: str
    event_id: str
    system_id: str
    event_type: str
    detected_at: datetime
    received_at: datetime


def retry_delay_seconds(failed_attempt: int) -> int:
    """The wait after failed attempt n (1 to 5) before attempt n + 1."""
    return RETRY_DELAYS_SECONDS[failed_attempt - 1]


def deliver_due(engine: Engine, post: webhooks.Poster | None = None) -> int:
    """Run one delivery pass. Returns the number of delivery attempts made."""
    send = post if post is not None else webhooks.post_json
    try:
        _route(engine)
    except Exception as exc:
        logger.warning("delivery routing failed: type=%s", type(exc).__name__)
    try:
        claims = _claim(engine)
    except Exception as exc:
        logger.warning("delivery claim failed: type=%s", type(exc).__name__)
        return 0
    for claim in claims:
        started = time.perf_counter()
        status = send(claim.url, _body(claim), webhooks.TIMEOUT_SECONDS)
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "delivery attempt: notification_id=%s webhook_id=%s attempt=%d status=%s"
            " duration_ms=%d",
            claim.notification_id,
            claim.webhook_id,
            claim.attempt,
            "none" if status is None else status,
            duration_ms,
        )
        try:
            _record(engine, claim, status)
        except Exception as exc:  # one bad row must never stop the pass (spec 3.3)
            # Rolled back: the row keeps its lease and is retried when the lease runs out.
            logger.warning(
                "delivery result not recorded: notification_id=%s webhook_id=%s type=%s",
                claim.notification_id,
                claim.webhook_id,
                type(exc).__name__,
            )
    return len(claims)


def _route(engine: Engine) -> None:
    """Spec 3.5 step 1: give each unrouted notification one PENDING delivery per matching
    webhook. Rows are locked with SKIP LOCKED, so concurrent passes route each one once."""
    with Session(engine) as session:
        now = clock.now()
        unrouted = session.execute(
            text(
                "SELECT n.notification_id, n.notification_type FROM notification n"
                " WHERE NOT EXISTS (SELECT 1 FROM notification_delivery d"
                " WHERE d.notification_id = n.notification_id)"
                " ORDER BY n.created_at, n.notification_id"
                " FOR UPDATE OF n SKIP LOCKED"
            )
        ).all()
        for row in unrouted:
            session.execute(
                text(
                    "INSERT INTO notification_delivery"
                    " (notification_id, webhook_id, status, attempt_count, next_attempt_at)"
                    " SELECT :notification_id, webhook_id, 'PENDING', 0, :now FROM webhook"
                    " WHERE :type = ANY(event_types)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"notification_id": row.notification_id, "type": row.notification_type, "now": now},
            )
        session.commit()


def _claim(engine: Engine) -> list[Claim]:
    """Take due PENDING rows whose lease is absent or expired, give each a new lease and count
    the attempt, and commit before any network call (spec 3.5 step 2)."""
    with Session(engine) as session:
        now = clock.now()
        rows = session.execute(
            text(
                "SELECT d.notification_id, d.webhook_id, d.attempt_count, w.url,"
                " n.notification_type, e.event_id, e.system_id, e.type, e.detected_at,"
                " e.received_at"
                " FROM notification_delivery d"
                " JOIN notification n ON n.notification_id = d.notification_id"
                " JOIN webhook w ON w.webhook_id = d.webhook_id"
                " JOIN event e ON e.event_id = n.event_id"
                " WHERE d.status = 'PENDING' AND d.next_attempt_at <= :now"
                " AND (d.lease_until IS NULL OR d.lease_until <= :now)"
                " ORDER BY d.next_attempt_at, d.notification_id, d.webhook_id"
                " LIMIT :limit FOR UPDATE OF d SKIP LOCKED"
            ),
            {"now": now, "limit": CLAIM_BATCH_SIZE},
        ).all()
        lease_until = now + timedelta(seconds=LEASE_SECONDS)
        claims = []
        for row in rows:
            attempt = row.attempt_count + 1
            session.execute(
                text(
                    "UPDATE notification_delivery SET attempt_count = :attempt,"
                    " lease_until = :lease_until"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {
                    "attempt": attempt,
                    "lease_until": lease_until,
                    "notification_id": row.notification_id,
                    "webhook_id": row.webhook_id,
                },
            )
            claims.append(
                Claim(
                    notification_id=row.notification_id,
                    webhook_id=row.webhook_id,
                    attempt=attempt,
                    lease_until=lease_until,
                    url=row.url,
                    notification_type=row.notification_type,
                    event_id=row.event_id,
                    system_id=row.system_id,
                    event_type=row.type,
                    detected_at=row.detected_at,
                    received_at=row.received_at,
                )
            )
        session.commit()
    return claims


def _body(claim: Claim) -> dict[str, Any]:
    """Exactly the spec 3.5 body. `type` is the event type (threshold_breach or monitoring_gap);
    sent_at is the moment of this attempt."""
    return {
        "notification_type": claim.notification_type,
        "notification_id": claim.notification_id,
        "event_id": claim.event_id,
        "system_id": claim.system_id,
        "type": claim.event_type,
        "detected_at": api_time(claim.detected_at),
        "sent_at": api_time(clock.now()),
    }


def _record(engine: Engine, claim: Claim, status: int | None) -> None:
    global _late_deliveries
    with Session(engine) as session:
        current = session.execute(
            text(
                "SELECT status, attempt_count, lease_until FROM notification_delivery"
                " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                " FOR UPDATE"
            ),
            {"notification_id": claim.notification_id, "webhook_id": claim.webhook_id},
        ).one_or_none()
        if (
            current is None  # emptied by a test reset while the call was in flight
            or current.status != "PENDING"
            or current.attempt_count != claim.attempt
            or current.lease_until != claim.lease_until
        ):
            # Another pass took this delivery over after our lease ran out; its result wins.
            session.rollback()
            return
        now = clock.now()
        keys = {"notification_id": claim.notification_id, "webhook_id": claim.webhook_id}
        last_status = None if status is None else str(status)
        if webhooks.is_success(status):
            latency = whole_seconds(claim.received_at, now)
            bound = config.notify_max_delay_seconds()
            within_bound = latency <= bound
            session.execute(
                text(
                    "UPDATE notification_delivery SET status = 'DELIVERED',"
                    " lease_until = NULL, last_status = :last_status, delivered_at = :now"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {**keys, "last_status": last_status, "now": now},
            )
            ledger.append(
                session,
                "NOTIFICATION_DELIVERED",
                WORKER_ACTOR,
                claim.event_id,
                {
                    **keys,
                    "attempt": claim.attempt,
                    "latency_seconds": latency,
                    "within_bound": within_bound,
                },
            )
            session.commit()
            if not within_bound:
                _late_deliveries += 1
                logger.warning(
                    "late delivery: notification_id=%s webhook_id=%s latency_seconds=%d"
                    " bound_seconds=%d",
                    claim.notification_id,
                    claim.webhook_id,
                    latency,
                    bound,
                )
            return
        if claim.attempt >= MAX_ATTEMPTS:
            session.execute(
                text(
                    "UPDATE notification_delivery SET status = 'FAILED',"
                    " lease_until = NULL, last_status = :last_status"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {**keys, "last_status": last_status},
            )
            ledger.append(
                session,
                "NOTIFICATION_FAILED",
                WORKER_ACTOR,
                claim.event_id,
                {**keys, "attempts": claim.attempt, "last_status": status},
            )
        else:
            session.execute(
                text(
                    "UPDATE notification_delivery SET lease_until = NULL,"
                    " last_status = :last_status, next_attempt_at = :next"
                    " WHERE notification_id = :notification_id AND webhook_id = :webhook_id"
                ),
                {
                    **keys,
                    "last_status": last_status,
                    "next": now + timedelta(seconds=retry_delay_seconds(claim.attempt)),
                },
            )
        session.commit()
