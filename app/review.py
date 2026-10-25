"""Review (Phase 4 Part 1): harm-field pre-fill at flag time, the review start and the
harm-field decisions of a disposition call (Phase 4 spec 1.3 to 1.5, Addendum rules 2 and 4).

prefill_harm_fields (spec 1.3), parse_harm_fields, require_complete and apply_harm_fields (spec
1.5, Addendum rule 2) are real. start_if_reviewer is still the STUB of the Phase 4 base commit,
with the behavior of spec section 11; Part 1 replaces it. The signatures, PrefillFacts,
HarmFieldDecision, ParsedHarmFields, HarmDecisions and the two exceptions keep their shape,
because Parts 2 and 3 build against them. parse_harm_fields and require_complete are pure
(Addendum rule 8).

Callers own transactions; nothing here commits.

Logging, on `api.review`, never with the evidence or the context text: the pre-fill writes one
DEBUG line with the event id and the counts of pre-filled and blank fields; apply_harm_fields
writes one INFO line per changed field with the event id, the key and the old and new status.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.errors import ApiError
from app.events import HARM_FIELDS
from app.formats import api_time

review_logger = logging.getLogger("api.review")

# Spec 1.3: the comparator word of the evidence template.
COMPARATOR_WORDS = {"gt": "above", "lt": "below"}
PENDING = "PREFILLED_PENDING_CONFIRMATION"
CONFIRMED = "CONFIRMED"
NOT_APPLICABLE = "NOT_APPLICABLE"
STATUSES = (PENDING, CONFIRMED, NOT_APPLICABLE)
# Spec 1.5: the longest evidence text a request may send.
EVIDENCE_MAX_LENGTH = 2000

UNCONFIRMED_MESSAGE = "all four harm fields must be CONFIRMED or NOT_APPLICABLE"


class HarmFieldsMalformed(ApiError):
    """400 MALFORMED_BODY: harm_fields has a shape no decision can be read from (spec 1.5,
    first row). Raised by parse_harm_fields."""

    def __init__(self) -> None:
        super().__init__(400, "MALFORMED_BODY", "the request is malformed")


class HarmFieldsUnconfirmed(ApiError):
    """409 HARM_FIELDS_UNCONFIRMED: a field is missing or still PREFILLED_PENDING_CONFIRMATION
    (spec 1.5, second row). Raised by require_complete."""

    def __init__(self) -> None:
        super().__init__(409, "HARM_FIELDS_UNCONFIRMED", UNCONFIRMED_MESSAGE)


@dataclass(frozen=True)
class PrefillFacts:
    """The event facts the evidence template uses (spec 1.3). Values are as stored on the
    event and its threshold version."""

    metric_name: str
    observed_value: Decimal
    threshold_value: Decimal
    comparator: str  # gt or lt
    threshold_version_id: str
    detected_at: datetime


@dataclass(frozen=True)
class HarmFieldDecision:
    """One field of a disposition request: its status and its evidence. evidence is None both
    when the request sent null and when it left evidence out (spec 1.5 treats them alike)."""

    status: str
    evidence: str | None


@dataclass(frozen=True)
class ParsedHarmFields:
    """parse_harm_fields' result. `fields` holds the keys the request sent, in HARM_FIELDS
    order, each with any of the three statuses. `complete` is true only when all four keys
    are present and each is CONFIRMED or NOT_APPLICABLE."""

    complete: bool
    fields: Mapping[str, HarmFieldDecision]


@dataclass(frozen=True)
class HarmDecisions:
    """require_complete's result: all four keys, in HARM_FIELDS order, each CONFIRMED or
    NOT_APPLICABLE. apply_harm_fields takes only this type, so an incomplete request cannot
    be written."""

    fields: Mapping[str, HarmFieldDecision]


def evidence_text(facts: PrefillFacts, context_text: str) -> str:
    """The pinned evidence template of spec 1.3, as one line: values as plain decimal strings
    (the same strings as the EVENT_FLAGGED payload), the time in the API format, one space
    before the context text."""
    return (
        f"{facts.metric_name} = {format(facts.observed_value, 'f')} is"
        f" {COMPARATOR_WORDS[facts.comparator]} the limit {format(facts.threshold_value, 'f')}"
        f" (threshold {facts.threshold_version_id}) at {api_time(facts.detected_at)}."
        f" {context_text}"
    )


def prefill_harm_fields(
    session: Session, event_id: str, system_id: str, facts: PrefillFacts
) -> None:
    """Insert the four harm fields of a new breach event (spec 1.3), in contract order.

    harm_context is read once for the system. A field with a context row gets the template text
    as both evidence and prefill_evidence; a field without one gets null for both, so nothing is
    guessed (AC-07). Every field is PREFILLED_PENDING_CONFIRMATION with no confirmer: the
    pre-fill never confirms a field or sets a tier (INV-3)."""
    contexts: dict[str, str] = {
        row.field_key: row.context_text
        for row in session.execute(
            text("SELECT field_key, context_text FROM harm_context WHERE system_id = :system_id"),
            {"system_id": system_id},
        )
    }
    fields = [
        {
            "event_id": event_id,
            "field_key": key,
            "status": PENDING,
            "evidence": evidence_text(facts, contexts[key]) if key in contexts else None,
        }
        for key in HARM_FIELDS
    ]
    session.execute(
        text(
            "INSERT INTO harm_field (event_id, field_key, status, evidence, prefill_evidence)"
            " VALUES (:event_id, :field_key, :status, :evidence, :evidence)"
        ),
        fields,
    )
    prefilled = sum(1 for field in fields if field["evidence"] is not None)
    review_logger.debug(
        "prefill: event_id=%s prefilled=%d blank=%d",
        event_id,
        prefilled,
        len(fields) - prefilled,
    )


def start_if_reviewer(session: Session, principal: Principal, event_id: str) -> bool:
    """Move a FLAGGED breach event to UNDER_REVIEW on a reviewer's view (spec 1.4, D3) and
    return whether the state changed. Appends nothing (Addendum rule 4).

    STUB for Phase 4 Part 1: returns False and changes nothing."""
    return False


def _is_malformed(value: Any) -> bool:
    """Whether one value of harm_fields is malformed (spec 1.5, first row): not an object, a key
    other than status or evidence, status missing or not one of the three, or evidence that is
    neither null nor a string of at most 2,000 characters without U+0000 or a lone surrogate
    (Part 1 decision G2: PostgreSQL cannot store one). Length counts code points."""
    if not isinstance(value, dict) or not set(value) <= {"status", "evidence"}:
        return True
    status = value.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        return True
    evidence = value.get("evidence")
    if evidence is None:
        return False
    if not isinstance(evidence, str) or len(evidence) > EVIDENCE_MAX_LENGTH:
        return True
    return any(char == "\x00" or "\ud800" <= char <= "\udfff" for char in evidence)


def parse_harm_fields(raw: Any) -> ParsedHarmFields:
    """Read the request's harm_fields (Addendum rule 2). Raises HarmFieldsMalformed for the
    malformed inputs of spec 1.5, checked on every value before completeness, so 400 wins over
    409. Absent or null harm_fields, a missing key or a pending status only make the result
    incomplete. Pure: no database, no clock."""
    if raw is None:
        return ParsedHarmFields(complete=False, fields={})
    if not isinstance(raw, dict) or not set(raw) <= set(HARM_FIELDS):
        raise HarmFieldsMalformed()
    if any(_is_malformed(value) for value in raw.values()):
        raise HarmFieldsMalformed()
    fields = {
        key: HarmFieldDecision(status=raw[key]["status"], evidence=raw[key].get("evidence"))
        for key in HARM_FIELDS
        if key in raw
    }
    complete = len(fields) == len(HARM_FIELDS) and all(
        decision.status != PENDING for decision in fields.values()
    )
    return ParsedHarmFields(complete=complete, fields=fields)


def require_complete(parsed: ParsedHarmFields) -> HarmDecisions:
    """The decisions of a complete request; raises HarmFieldsUnconfirmed otherwise (Addendum
    rule 2). Pure."""
    if not parsed.complete:
        raise HarmFieldsUnconfirmed()
    return HarmDecisions(fields=dict(parsed.fields))


def apply_harm_fields(
    session: Session,
    event_id: str,
    principal: Principal,
    decisions: HarmDecisions,
    now: datetime,
) -> list[str]:
    """Write the decisions to the event's harm fields and return the keys that changed, in
    contract order (spec 1.5). The caller holds the event row lock; the four harm_field rows
    are locked next, in field_key order.

    CONFIRMED with text stores the text and CONFIRMED with null stores the field's
    prefill_evidence; NOT_APPLICABLE stores the text or null. A field changes when its status
    or its stored evidence differs from what is stored, and only a changed field gets the
    caller as confirmer and `now` as its time. prefill_evidence is never written, and nothing
    here touches the event row or the ledger. One INFO line per changed field carries the
    statuses, never the evidence."""
    stored = {
        row.field_key: row
        for row in session.execute(
            text(
                "SELECT field_key, status, evidence, prefill_evidence FROM harm_field"
                " WHERE event_id = :event_id ORDER BY field_key FOR UPDATE"
            ),
            {"event_id": event_id},
        )
    }
    changed: list[str] = []
    updates: list[dict[str, Any]] = []
    for key in HARM_FIELDS:
        decision = decisions.fields[key]
        current = stored[key]
        evidence = decision.evidence
        if decision.status == CONFIRMED and evidence is None:
            evidence = current.prefill_evidence
        if (decision.status, evidence) == (current.status, current.evidence):
            continue
        changed.append(key)
        updates.append(
            {
                "event_id": event_id,
                "field_key": key,
                "status": decision.status,
                "evidence": evidence,
                "actor_id": principal.actor_id,
                "now": now,
            }
        )
        review_logger.info(
            "harm field changed: event_id=%s field_key=%s old_status=%s new_status=%s",
            event_id,
            key,
            current.status,
            decision.status,
        )
    if updates:
        session.execute(
            text(
                "UPDATE harm_field SET status = :status, evidence = :evidence,"
                " confirmed_by = :actor_id, confirmed_at = :now"
                " WHERE event_id = :event_id AND field_key = :field_key"
            ),
            updates,
        )
    return changed
