"""Review (Phase 4 Part 1): harm-field pre-fill at flag time, the review start and the
harm-field decisions of a disposition call (Phase 4 spec 1.3 to 1.5, Addendum rules 2 and 4).

STUB for Phase 4 Part 1: written by the Phase 4 base commit with the behavior of spec section
11 (Addendum rule 2 for parse_harm_fields and require_complete). Part 1 replaces every function
body in this module. The signatures, PrefillFacts, HarmFieldDecision, ParsedHarmFields,
HarmDecisions and the two exceptions keep their shape, because Parts 2 and 3 build against them.

Callers own transactions; nothing here commits.
"""

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


def prefill_harm_fields(
    session: Session, event_id: str, system_id: str, facts: PrefillFacts
) -> None:
    """Insert the four harm fields of a new breach event (spec 1.3).

    STUB for Phase 4 Part 1: the Phase 3 behavior, four PREFILLED_PENDING_CONFIRMATION rows with
    null evidence; system_id and facts are not read yet."""
    session.execute(
        text(
            "INSERT INTO harm_field (event_id, field_key, status)"
            " VALUES (:event_id, :field_key, 'PREFILLED_PENDING_CONFIRMATION')"
        ),
        [{"event_id": event_id, "field_key": key} for key in HARM_FIELDS],
    )


def start_if_reviewer(session: Session, principal: Principal, event_id: str) -> bool:
    """Move a FLAGGED breach event to UNDER_REVIEW on a reviewer's view (spec 1.4, D3) and
    return whether the state changed. Appends nothing (Addendum rule 4).

    STUB for Phase 4 Part 1: returns False and changes nothing."""
    return False


def parse_harm_fields(raw: Any) -> ParsedHarmFields:
    """Read the request's harm_fields (Addendum rule 2). Raises HarmFieldsMalformed for the
    malformed inputs of spec 1.5; absent, missing or pending fields only make it incomplete.

    STUB for Phase 4 Part 1: accepts any object with the four keys and marks it complete."""
    if not isinstance(raw, dict) or set(raw) != set(HARM_FIELDS):
        raise HarmFieldsMalformed()
    return ParsedHarmFields(
        complete=True,
        fields={
            key: HarmFieldDecision(status=raw[key]["status"], evidence=raw[key].get("evidence"))
            for key in HARM_FIELDS
        },
    )


def require_complete(parsed: ParsedHarmFields) -> HarmDecisions:
    """The decisions of a complete request; raises HarmFieldsUnconfirmed otherwise (Addendum
    rule 2).

    STUB for Phase 4 Part 1: returns the decisions."""
    return HarmDecisions(fields=parsed.fields)


def apply_harm_fields(
    session: Session,
    event_id: str,
    principal: Principal,
    decisions: HarmDecisions,
    now: datetime,
) -> list[str]:
    """Write the decisions to the event's harm fields and return the keys that changed
    (spec 1.5). The caller holds the event row lock.

    STUB for Phase 4 Part 1: writes the given statuses and evidence with the caller as
    confirmer and returns every key."""
    session.execute(
        text(
            "UPDATE harm_field SET status = :status, evidence = :evidence,"
            " confirmed_by = :actor_id, confirmed_at = :now"
            " WHERE event_id = :event_id AND field_key = :field_key"
        ),
        [
            {
                "event_id": event_id,
                "field_key": key,
                "status": decision.status,
                "evidence": decision.evidence,
                "actor_id": principal.actor_id,
                "now": now,
            }
            for key, decision in decisions.fields.items()
        ],
    )
    return list(decisions.fields)
