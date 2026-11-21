"""Erasure after the retention period (Phase 5 spec 4.4, 5.2; REQ-16, AC-18, INV-10, INV-11).

`erase` renders the personal data of one event permanently unreadable, in the caller's
transaction, after its `retain_until`. It never edits an append-only row and never changes a
ledger field, so no hash changes:

1. the event's record key is destroyed (every sealed value of the event is then unreadable: the
   disposition rationale, the ledger payloads that carry it, the report bytes);
2. the evidence text of the event's harm fields (mutable, stored plain) is set to null;
3. event.erased_at is set, which the report routes and events.detail read;
4. one RECORD_ERASED entry is appended (field names only, never values).

Erasure is operator-triggered, not automatic (D8), and refused before `retain_until`: that is
how INV-10 ("no event record or its evidence bundle is removed before retain_until") is enforced.
Nothing is purged (D9): the event, its metric values, states, hashes and ledger entries stay.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, ledger, personal_data
from app.auth import Principal
from app.errors import ApiError, not_found
from app.formats import api_time
from app.ledger.canonical import JsonValue

retention_logger = logging.getLogger("api.erasure")

NOT_EXPIRED_MESSAGE = "the retention period of this record has not ended"
ALREADY_ERASED_MESSAGE = "this record has already been erased"
# A disposition on an erased event (decision 287): the same code, ALREADY_ERASED.
ERASED_EVENT_MESSAGE = "the personal data of this event has been erased"


def _any(session: Session, sql: str, event_id: str) -> bool:
    return bool(session.execute(text(sql), {"event_id": event_id}).scalar_one())


def _sealed_fields(session: Session, event_id: str) -> list[str]:
    """Names of the stored fields of the event that hold a sealed value now (so a field that
    never held personal data is not listed) or plain evidence text that erasure is about to
    null. Field names only, never values."""
    sealed = personal_data.PREFIX
    checks = (
        (
            "disposition.rationale",
            "SELECT EXISTS (SELECT 1 FROM disposition WHERE event_id = :event_id"
            f" AND rationale LIKE '{sealed}%')",
        ),
        (
            "ledger_entry.payload.rationale",
            "SELECT EXISTS (SELECT 1 FROM ledger_entry WHERE subject_id = :event_id"
            f" AND payload->>'rationale' LIKE '{sealed}%')",
        ),
        (
            "harm_field.evidence",
            "SELECT EXISTS (SELECT 1 FROM harm_field WHERE event_id = :event_id"
            " AND evidence IS NOT NULL)",
        ),
        (
            "harm_field.prefill_evidence",
            "SELECT EXISTS (SELECT 1 FROM harm_field WHERE event_id = :event_id"
            " AND prefill_evidence IS NOT NULL)",
        ),
        (
            "report.json_bytes",
            "SELECT EXISTS (SELECT 1 FROM report WHERE event_id = :event_id"
            " AND substring(json_bytes from 1 for 4) = 'pd1:'::bytea)",
        ),
        (
            "report.pdf_bytes",
            "SELECT EXISTS (SELECT 1 FROM report WHERE event_id = :event_id"
            " AND substring(pdf_bytes from 1 for 4) = 'pd1:'::bytea)",
        ),
    )
    return [name for name, sql in checks if _any(session, sql, event_id)]


def erase(session: Session, principal: Principal, event_id: str) -> dict[str, JsonValue]:
    """Steps 4 to 7 of spec 4.4. Raises 404, 409 RETENTION_NOT_EXPIRED or 409 ALREADY_ERASED
    before anything is written; the caller commits on success."""
    event = session.execute(
        text("SELECT retain_until, erased_at FROM event WHERE event_id = :event_id FOR UPDATE"),
        {"event_id": event_id},
    ).one_or_none()
    if event is None:
        raise not_found()
    now = clock.now()
    if now < event.retain_until:
        raise ApiError(409, "RETENTION_NOT_EXPIRED", NOT_EXPIRED_MESSAGE)
    if event.erased_at is not None:
        raise ApiError(409, "ALREADY_ERASED", ALREADY_ERASED_MESSAGE)

    erased_fields = _sealed_fields(session, event_id)
    key_destroyed = personal_data.destroy(session, event_id, now)
    session.execute(
        text(
            "UPDATE harm_field SET evidence = NULL, prefill_evidence = NULL"
            " WHERE event_id = :event_id"
        ),
        {"event_id": event_id},
    )
    session.execute(
        text("UPDATE event SET erased_at = :now WHERE event_id = :event_id"),
        {"now": now, "event_id": event_id},
    )
    fields: list[JsonValue] = list(erased_fields)
    ledger.append(
        session,
        "RECORD_ERASED",
        principal.actor_id,
        event_id,
        {
            "event_id": event_id,
            "erased_at": api_time(now),
            "retain_until": api_time(event.retain_until),
            "key_destroyed": key_destroyed,
            "erased_fields": fields,
        },
    )
    return {"event_id": event_id, "erased_at": api_time(now), "erased_fields": fields}
