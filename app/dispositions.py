"""Dispositions (Phase 4 Part 2): the disposition and revision history of an event (Phase 4
spec 2.4 to 2.10; Addendum rule 1).

- parse_request(raw): the request body's shape (spec 2.3, step 2) and the NOT_SERIOUS rationale
  rule (step 3). Pure; the route calls it after authentication and before any database read.
- record(session, principal, event_id, request): steps 4 to 15 in the caller's transaction. The
  route commits (step 16) and returns events.detail.
- summary(session, event_id): {disposition, revisions, deadline_utc} in the contract's Event
  form (spec 2.7), which events.detail places in the Event object. Part 3 reads it when it
  fires a reminder (Addendum rule 3).
- report_guard(session, event_id): the report's 404 and 409 NO_DISPOSITION (spec 2.8).

The rows are the history and are never edited (INV-4): a revision appends a row whose
supersedes_id is the previous row. event.state and event.deadline_utc hold the current values.
The deadline is always compute(anchor, tier, calendar) with the first row's anchor and
calendar, so a revision never moves the anchor and a configuration change never moves an
existing event's deadline (spec 2.5, 6).

Callers own transactions; nothing here commits. escalations is imported inside record, because
Part 3's escalations imports summary (Addendum 9.6).

Logging, on `api.disposition`, never with the rationale or evidence text: the route writes one
INFO line per recorded disposition after its commit.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, deadlines, ledger, review
from app.auth import Principal
from app.errors import ApiError, not_found
from app.events import HARM_FIELDS
from app.formats import api_time, new_id
from app.ledger.canonical import JsonValue, Payload

disposition_logger = logging.getLogger("api.disposition")

# Spec 2.3: the longest rationale a request may send, counted in code points before trimming.
RATIONALE_MAX_LENGTH = 4000

RATIONALE_REQUIRED_MESSAGE = "rationale is required when tier is NOT_SERIOUS"
EXTENSION_MESSAGE = "extending a running deadline requires a rationale"
NOT_REVIEWABLE_MESSAGE = "a monitoring gap event cannot be dispositioned"
NO_DISPOSITION_MESSAGE = "no disposition has been recorded for this event"


def malformed() -> ApiError:
    return ApiError(400, "MALFORMED_BODY", "the request is malformed")


@dataclass(frozen=True)
class DispositionRequest:
    """A disposition request whose shape is valid (spec 2.3). rationale is trimmed and None when
    blank; harm_fields may still be incomplete (409 at step 7)."""

    tier: str
    rationale: str | None
    harm_fields: review.ParsedHarmFields


@dataclass(frozen=True)
class Recorded:
    """What record wrote, for the route's log line."""

    disposition_id: str
    seq_in_event: int
    tier: str
    revision: bool


def _bad_text(value: str) -> bool:
    """U+0000 or a lone surrogate: PostgreSQL text cannot hold either (Part 2 decision 178)."""
    return any(char == "\x00" or "\ud800" <= char <= "\udfff" for char in value)


def parse_request(raw: Any) -> DispositionRequest:
    """Steps 2 and 3 of spec 2.4. Raises 400 MALFORMED_BODY for a body that is not an object,
    a tier that is not exactly one of the four contract strings, a rationale that is not null or
    a string of at most 4,000 code points without U+0000 or a lone surrogate, or a malformed
    harm_fields (Part 1's parse_harm_fields); then 400 RATIONALE_REQUIRED for NOT_SERIOUS with a
    blank rationale (absent, null or only whitespace). Unknown top-level keys are ignored.
    Pure."""
    if not isinstance(raw, dict):
        raise malformed()
    tier = raw.get("tier")
    if not isinstance(tier, str) or tier not in deadlines.TIERS:
        raise malformed()
    rationale = raw.get("rationale")
    if rationale is not None and (
        not isinstance(rationale, str)
        or len(rationale) > RATIONALE_MAX_LENGTH
        or _bad_text(rationale)
    ):
        raise malformed()
    harm_fields = review.parse_harm_fields(raw.get("harm_fields"))
    trimmed = None if rationale is None else rationale.strip() or None
    if tier == deadlines.NOT_SERIOUS and trimmed is None:
        raise ApiError(400, "RATIONALE_REQUIRED", RATIONALE_REQUIRED_MESSAGE)
    return DispositionRequest(tier=tier, rationale=trimmed, harm_fields=harm_fields)


def _time(moment: datetime | None) -> str | None:
    return None if moment is None else api_time(moment)


def _rows(session: Session, event_id: str) -> list[Any]:
    return list(
        session.execute(
            text(
                "SELECT disposition_id, seq_in_event, tier, rationale, reviewer_id, assigned_at,"
                " anchor_at, calendar, deadline_utc FROM disposition WHERE event_id = :event_id"
                " ORDER BY seq_in_event"
            ),
            {"event_id": event_id},
        )
    )


def record(
    session: Session, principal: Principal, event_id: str, request: DispositionRequest
) -> Recorded:
    """Steps 4 to 15 of spec 2.4, in the caller's transaction: lock the event row, run the
    remaining checks (404, 409 EVENT_NOT_REVIEWABLE, 409 HARM_FIELDS_UNCONFIRMED, 409
    DEADLINE_EXTENSION_REQUIRES_RATIONALE), then write the harm fields, the disposition row,
    the event row and the escalation plan, and append one ledger entry last. Any exception
    leaves the caller to roll everything back; a check that fails has written nothing."""
    from app import escalations  # inside the function: escalations imports this module

    event = session.execute(
        text("SELECT type, deadline_utc FROM event WHERE event_id = :event_id FOR UPDATE"),
        {"event_id": event_id},
    ).one_or_none()
    if event is None:
        raise not_found()
    if event.type == "monitoring_gap":
        raise ApiError(409, "EVENT_NOT_REVIEWABLE", NOT_REVIEWABLE_MESSAGE)
    # Read after the lock: at READ COMMITTED this statement sees every row committed by an
    # earlier holder of the lock, so a second call becomes a revision of the first.
    rows = _rows(session, event_id)
    decisions = review.require_complete(request.harm_fields)
    prior_tiers = [row.tier for row in rows]
    if request.rationale is None and deadlines.extends_deadline(prior_tiers, request.tier):
        raise ApiError(409, "DEADLINE_EXTENSION_REQUIRES_RATIONALE", EXTENSION_MESSAGE)

    now = clock.now()
    if rows:
        first, previous = rows[0], rows[-1]
        anchor, calendar = first.anchor_at, first.calendar
    else:
        previous = None
        anchor, calendar = now, deadlines.current_calendar()
    deadline_utc = deadlines.compute(anchor, request.tier, calendar)

    changed = review.apply_harm_fields(session, event_id, principal, decisions, now)

    disposition_id = new_id("dsp")
    seq_in_event = 1 if previous is None else previous.seq_in_event + 1
    session.execute(
        text(
            "INSERT INTO disposition (disposition_id, event_id, seq_in_event, tier, rationale,"
            " reviewer_id, assigned_at, anchor_at, calendar, deadline_utc, supersedes_id)"
            " VALUES (:disposition_id, :event_id, :seq_in_event, :tier, :rationale,"
            " :reviewer_id, :now, :anchor, :calendar, :deadline_utc, :supersedes_id)"
        ),
        {
            "disposition_id": disposition_id,
            "event_id": event_id,
            "seq_in_event": seq_in_event,
            "tier": request.tier,
            "rationale": request.rationale,
            "reviewer_id": principal.actor_id,
            "now": now,
            "anchor": anchor,
            "calendar": calendar,
            "deadline_utc": deadline_utc,
            "supersedes_id": None if previous is None else previous.disposition_id,
        },
    )
    state = "CLOSED" if request.tier == deadlines.NOT_SERIOUS else "DISPOSITIONED"
    session.execute(
        text(
            "UPDATE event SET state = :state, deadline_utc = :deadline_utc"
            " WHERE event_id = :event_id"
        ),
        {"state": state, "deadline_utc": deadline_utc, "event_id": event_id},
    )
    escalations.plan(session, event_id, anchor, deadline_utc)

    statuses: dict[str, JsonValue] = {key: decisions.fields[key].status for key in HARM_FIELDS}
    harm_fields_changed: list[JsonValue] = list(changed)
    common: Payload = {
        "rationale": request.rationale,
        "deadline_utc": _time(deadline_utc),
        "anchor_at": api_time(anchor),
        "calendar": calendar,
        "harm_fields": statuses,
    }
    if previous is None:
        ledger.append(
            session,
            "DISPOSITION_RECORDED",
            principal.actor_id,
            event_id,
            {
                "event_id": event_id,
                "disposition_id": disposition_id,
                "tier": request.tier,
                **common,
            },
        )
    else:
        ledger.append(
            session,
            "DISPOSITION_REVISED",
            principal.actor_id,
            event_id,
            {
                "event_id": event_id,
                "disposition_id": disposition_id,
                "supersedes_id": previous.disposition_id,
                "prior_tier": previous.tier,
                "new_tier": request.tier,
                "prior_deadline_utc": _time(previous.deadline_utc),
                **common,
                "harm_fields_changed": harm_fields_changed,
            },
        )
    return Recorded(disposition_id, seq_in_event, request.tier, previous is not None)


def summary(session: Session, event_id: str) -> dict[str, Any]:
    """{disposition, revisions, deadline_utc} for an event, in the contract's Event form (times
    as API strings), as events.detail puts them in the Event object (spec 2.7).

    disposition is null without a row; otherwise tier and rationale come from the latest row
    (the decision in force) and reviewer_id and assigned_at from the first (the assignment that
    anchors the deadline). revisions has one entry per row after the first, oldest first: the
    previous row's tier, this row's tier, reviewer and assigned_at. deadline_utc is the event
    row's (null for NOT_SERIOUS and for an unknown event)."""
    rows = _rows(session, event_id)
    deadline = session.execute(
        text("SELECT deadline_utc FROM event WHERE event_id = :event_id"),
        {"event_id": event_id},
    ).scalar_one_or_none()
    disposition = None
    if rows:
        disposition = {
            "tier": rows[-1].tier,
            "rationale": rows[-1].rationale,
            "reviewer_id": rows[0].reviewer_id,
            "assigned_at": api_time(rows[0].assigned_at),
        }
    revisions = [
        {
            "prior_tier": previous.tier,
            "new_tier": row.tier,
            "reviewer_id": row.reviewer_id,
            "revised_at": api_time(row.assigned_at),
        }
        for previous, row in zip(rows, rows[1:], strict=False)
    ]
    return {"disposition": disposition, "revisions": revisions, "deadline_utc": _time(deadline)}


def report_guard(session: Session, event_id: str) -> None:
    """Steps 2 and 3 of spec 2.8: 404 for an unknown event, then 409 NO_DISPOSITION when no
    disposition row exists (REQ-08a; gap events always land here). Reads only."""
    found = session.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM disposition WHERE disposition.event_id = event.event_id)"
            " AS disposed FROM event WHERE event_id = :event_id"
        ),
        {"event_id": event_id},
    ).one_or_none()
    if found is None:
        raise not_found()
    if not found.disposed:
        raise ApiError(409, "NO_DISPOSITION", NO_DISPOSITION_MESSAGE)
