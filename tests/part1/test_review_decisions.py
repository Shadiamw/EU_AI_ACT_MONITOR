"""Phase 4 Part 1: the harm-field decisions of a disposition call (Phase 4 spec 1.5, Addendum
rule 2, AC-07 confirmation record).

The first half is the validation matrix of spec 1.5 on the two pure functions: every malformed
input of the first row is 400 MALFORMED_BODY, every incomplete input of the second row parses
with complete False and require_complete turns it into 409 HARM_FIELDS_UNCONFIRMED, and 400 wins
over incompleteness. A lone UTF-16 surrogate in evidence is malformed too (Part 1 decision G2).
Error bodies are checked through app.errors.error_response, the function that writes them.

The second half runs apply_harm_fields on the database, as Part 2 will: in a session of the
application role, with the event row locked first and a fixed `now`. The event is spec 1.3's
AC-07 breach, so the rights field carries a pre-fill and the other three are blank.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth import Principal
from app.errors import ApiError, error_response
from app.review import (
    HarmDecisions,
    HarmFieldDecision,
    HarmFieldsMalformed,
    HarmFieldsUnconfirmed,
    apply_harm_fields,
    parse_harm_fields,
    require_complete,
)
from tests.api.conftest import MALFORMED, ApiClient
from tests.db_helpers import HARM_FIELDS
from tests.part1.conftest import entry_types, get, rows
from tests.part1.test_review_prefill import AC07_EVIDENCE, RIGHTS, post_ac07, stored_fields
from tests.worker.conftest import FakeClock

HEALTH, INFRASTRUCTURE, _, PROPERTY = HARM_FIELDS
UNCONFIRMED = {
    "error_code": "HARM_FIELDS_UNCONFIRMED",
    "message": "all four harm fields must be CONFIRMED or NOT_APPLICABLE",
}
NOW = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
# Contract Example 2's deadline, set on the event before a revision.
DEADLINE = datetime(2026, 10, 12, 10, 12, tzinfo=UTC)
REV_1 = Principal(role="REVIEWER", actor_id="rev-1", system_id=None)
REV_2 = Principal(role="REVIEWER", actor_id="rev-2", system_id=None)


def body(error: ApiError) -> dict[str, Any]:
    """The response body the API writes for this error."""
    response = error_response(error.status, error.error_code, error.message)
    parsed: dict[str, Any] = json.loads(bytes(response.body))
    return parsed


def confirmed(evidence: Any = None) -> dict[str, Any]:
    return {"status": "CONFIRMED", "evidence": evidence}


def not_applicable(evidence: Any = None) -> dict[str, Any]:
    return {"status": "NOT_APPLICABLE", "evidence": evidence}


def ac07(**changes: Any) -> dict[str, Any]:
    """Spec 1.6's AC-07 confirmation: rights CONFIRMED keeping its pre-fill, the other three
    NOT_APPLICABLE with no evidence, with some fields changed."""
    return {
        HEALTH: not_applicable(),
        INFRASTRUCTURE: not_applicable(),
        RIGHTS: confirmed(),
        PROPERTY: not_applicable(),
        **changes,
    }


# ---------------------------------------------------------------------------------------------
# Validation matrix, 400 half (spec 1.5, first row)
# ---------------------------------------------------------------------------------------------

MALFORMED_INPUTS = {
    # harm_fields present but not an object
    "a list": [],
    "a string": "CONFIRMED",
    "a number": 4,
    "true": True,
    # a key that is not one of the four
    "a fifth key": ac07(other_harm=not_applicable()),
    "only an unknown key": {"other_harm": not_applicable()},
    "a key in the wrong case": {**ac07(), "Health_Safety_Harm": not_applicable()},
    # a value that is not an object
    "a string value": ac07(**{RIGHTS: "CONFIRMED"}),
    "a list value": ac07(**{RIGHTS: ["CONFIRMED", None]}),
    "a null value": ac07(**{RIGHTS: None}),
    # a value with a key other than status or evidence
    "an extra value key": ac07(**{RIGHTS: {**confirmed(), "note": "x"}}),
    "confirmed_by sent": ac07(**{RIGHTS: {**confirmed(), "confirmed_by": "rev-1"}}),
    # status missing or not one of the three strings
    "no status": ac07(**{RIGHTS: {"evidence": "text"}}),
    "an empty value object": ac07(**{RIGHTS: {}}),
    "a lowercase status": ac07(**{RIGHTS: {"status": "confirmed"}}),
    "an unknown status": ac07(**{RIGHTS: {"status": "REJECTED"}}),
    "an empty status": ac07(**{RIGHTS: {"status": ""}}),
    "a null status": ac07(**{RIGHTS: {"status": None}}),
    "a numeric status": ac07(**{RIGHTS: {"status": 1}}),
    "a list status": ac07(**{RIGHTS: {"status": ["CONFIRMED"]}}),
    # evidence neither a string nor null
    "numeric evidence": ac07(**{RIGHTS: confirmed(5)}),
    "boolean evidence": ac07(**{RIGHTS: confirmed(False)}),
    "list evidence": ac07(**{RIGHTS: confirmed(["text"])}),
    "object evidence": ac07(**{RIGHTS: confirmed({"text": "x"})}),
    # evidence over 2,000 characters or containing U+0000
    "2,001 characters": ac07(**{RIGHTS: confirmed("e" * 2001)}),
    "2,001 characters on NOT_APPLICABLE": ac07(**{HEALTH: not_applicable("e" * 2001)}),
    "U+0000": ac07(**{RIGHTS: confirmed("before\x00after")}),
    "only U+0000": ac07(**{RIGHTS: confirmed("\x00")}),
    # Part 1 decision G2: a lone UTF-16 surrogate (PostgreSQL cannot store it)
    "a lone high surrogate": ac07(**{RIGHTS: confirmed("text \ud800 text")}),
    "a lone low surrogate": ac07(**{RIGHTS: confirmed("\udfff")}),
    # 400 wins over incompleteness
    "malformed with a key missing": {
        HEALTH: not_applicable(),
        INFRASTRUCTURE: confirmed(5),
    },
    "malformed with a pending status": ac07(
        **{
            HEALTH: {"status": "PREFILLED_PENDING_CONFIRMATION", "evidence": None},
            PROPERTY: not_applicable("\x00"),
        }
    ),
    "a fifth key with a key missing": {RIGHTS: confirmed(), "other_harm": not_applicable()},
    "a pending value with an extra key": ac07(
        **{RIGHTS: {"status": "PREFILLED_PENDING_CONFIRMATION", "evidence": None, "x": 1}}
    ),
}


@pytest.mark.parametrize("raw", MALFORMED_INPUTS.values(), ids=MALFORMED_INPUTS.keys())
def test_each_malformed_input_is_400_malformed_body(raw: Any) -> None:
    with pytest.raises(HarmFieldsMalformed) as excinfo:
        parse_harm_fields(raw)
    assert excinfo.value.status == 400
    assert body(excinfo.value) == MALFORMED


ACCEPTED_EVIDENCE = {
    "exactly 2,000 characters": "e" * 2000,
    "2,000 code points outside the BMP": "\U0001f600" * 2000,
    "an empty string": "",
    "only spaces": "   ",
    "a valid surrogate pair's character": "harm \U0001f6a8 noted",
    "U+0001": "\x01",
}


@pytest.mark.parametrize("evidence", ACCEPTED_EVIDENCE.values(), ids=ACCEPTED_EVIDENCE.keys())
def test_evidence_at_or_under_the_limits_is_accepted_as_given(evidence: str) -> None:
    parsed = parse_harm_fields(
        ac07(**{RIGHTS: confirmed(evidence), HEALTH: not_applicable(evidence)})
    )
    assert parsed.complete is True
    assert parsed.fields[RIGHTS] == HarmFieldDecision(status="CONFIRMED", evidence=evidence)
    assert parsed.fields[HEALTH] == HarmFieldDecision(status="NOT_APPLICABLE", evidence=evidence)


# ---------------------------------------------------------------------------------------------
# Validation matrix, 409 half (spec 1.5, second row; Addendum rule 2)
# ---------------------------------------------------------------------------------------------

INCOMPLETE_INPUTS: dict[str, tuple[Any, dict[str, HarmFieldDecision]]] = {
    "absent or null": (None, {}),
    "an empty object": ({}, {}),
    "a missing key": (
        {RIGHTS: confirmed(), HEALTH: not_applicable("n/a"), PROPERTY: not_applicable()},
        {
            HEALTH: HarmFieldDecision("NOT_APPLICABLE", "n/a"),
            RIGHTS: HarmFieldDecision("CONFIRMED", None),
            PROPERTY: HarmFieldDecision("NOT_APPLICABLE", None),
        },
    ),
    "a PREFILLED status": (
        ac07(**{INFRASTRUCTURE: {"status": "PREFILLED_PENDING_CONFIRMATION"}}),
        {
            HEALTH: HarmFieldDecision("NOT_APPLICABLE", None),
            INFRASTRUCTURE: HarmFieldDecision("PREFILLED_PENDING_CONFIRMATION", None),
            RIGHTS: HarmFieldDecision("CONFIRMED", None),
            PROPERTY: HarmFieldDecision("NOT_APPLICABLE", None),
        },
    ),
    "every status PREFILLED": (
        {key: {"status": "PREFILLED_PENDING_CONFIRMATION", "evidence": "x"} for key in HARM_FIELDS},
        {key: HarmFieldDecision("PREFILLED_PENDING_CONFIRMATION", "x") for key in HARM_FIELDS},
    ),
}


@pytest.mark.parametrize(
    ("raw", "fields"), INCOMPLETE_INPUTS.values(), ids=INCOMPLETE_INPUTS.keys()
)
def test_each_incomplete_input_parses_with_complete_false(
    raw: Any, fields: dict[str, HarmFieldDecision]
) -> None:
    parsed = parse_harm_fields(raw)
    assert parsed.complete is False
    assert list(parsed.fields.items()) == list(fields.items())


@pytest.mark.parametrize(
    "raw", [raw for raw, _ in INCOMPLETE_INPUTS.values()], ids=INCOMPLETE_INPUTS.keys()
)
def test_require_complete_turns_each_incomplete_input_into_409(raw: Any) -> None:
    parsed = parse_harm_fields(raw)
    with pytest.raises(HarmFieldsUnconfirmed) as excinfo:
        require_complete(parsed)
    assert excinfo.value.status == 409
    assert body(excinfo.value) == UNCONFIRMED


def test_a_complete_input_gives_the_four_decisions_in_contract_order() -> None:
    # Keys sent in reverse order; evidence omitted on one field, null on another.
    raw = {
        PROPERTY: {"status": "NOT_APPLICABLE"},
        RIGHTS: confirmed("reviewer text"),
        INFRASTRUCTURE: not_applicable(None),
        HEALTH: {"evidence": None, "status": "CONFIRMED"},
    }
    parsed = parse_harm_fields(raw)
    assert parsed.complete is True
    decisions = require_complete(parsed)
    assert isinstance(decisions, HarmDecisions)
    assert list(decisions.fields.items()) == [
        (HEALTH, HarmFieldDecision("CONFIRMED", None)),
        (INFRASTRUCTURE, HarmFieldDecision("NOT_APPLICABLE", None)),
        (RIGHTS, HarmFieldDecision("CONFIRMED", "reviewer text")),
        (PROPERTY, HarmFieldDecision("NOT_APPLICABLE", None)),
    ]


def test_parsing_does_not_change_the_request() -> None:
    raw = ac07(**{RIGHTS: confirmed("text")})
    before = json.dumps(raw, sort_keys=True)
    require_complete(parse_harm_fields(raw))
    assert json.dumps(raw, sort_keys=True) == before


# ---------------------------------------------------------------------------------------------
# apply_harm_fields on the database (spec 1.5, second table)
# ---------------------------------------------------------------------------------------------


def apply(
    app_engine: Engine,
    event_id: str,
    raw: dict[str, Any],
    principal: Principal = REV_1,
    now: datetime = NOW,
) -> list[str]:
    """One disposition's harm-field write, as Part 2 makes it: event row lock, then the write,
    then commit."""
    decisions = require_complete(parse_harm_fields(raw))
    with Session(app_engine) as session:
        session.execute(
            text("SELECT event_id FROM event WHERE event_id = :event_id FOR UPDATE"),
            {"event_id": event_id},
        )
        changed = apply_harm_fields(session, event_id, principal, decisions, now)
        session.commit()
    return changed


def by_key(owner_engine: Engine, event_id: str) -> dict[str, dict[str, Any]]:
    return {row.pop("field_key"): row for row in stored_fields(owner_engine, event_id)}


def field(
    status: str,
    evidence: str | None,
    prefill: str | None,
    by: str | None,
    at: datetime | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "evidence": evidence,
        "prefill_evidence": prefill,
        "confirmed_by": by,
        "confirmed_at": at,
    }


@pytest.fixture
def event_id(client: ApiClient, fake_clock: FakeClock) -> str:
    return post_ac07(client, fake_clock)


def test_ac07_confirmation_record(owner_engine: Engine, app_engine: Engine, event_id: str) -> None:
    assert apply(app_engine, event_id, ac07()) == list(HARM_FIELDS)
    assert by_key(owner_engine, event_id) == {
        HEALTH: field("NOT_APPLICABLE", None, None, "rev-1", NOW),
        INFRASTRUCTURE: field("NOT_APPLICABLE", None, None, "rev-1", NOW),
        RIGHTS: field("CONFIRMED", AC07_EVIDENCE, AC07_EVIDENCE, "rev-1", NOW),
        PROPERTY: field("NOT_APPLICABLE", None, None, "rev-1", NOW),
    }


def test_an_evidence_override_is_stored_and_null_restores_the_prefill(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    assert apply(app_engine, event_id, ac07(**{RIGHTS: confirmed("reviewer text")})) == list(
        HARM_FIELDS
    )
    assert by_key(owner_engine, event_id)[RIGHTS] == field(
        "CONFIRMED", "reviewer text", AC07_EVIDENCE, "rev-1", NOW
    )
    # CONFIRMED with null over the override puts the pre-fill back, which is a change.
    assert apply(app_engine, event_id, ac07(), principal=REV_2, now=LATER) == [RIGHTS]
    assert by_key(owner_engine, event_id)[RIGHTS] == field(
        "CONFIRMED", AC07_EVIDENCE, AC07_EVIDENCE, "rev-2", LATER
    )


def test_confirmed_with_evidence_omitted_stores_the_prefill(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    apply(app_engine, event_id, ac07(**{RIGHTS: {"status": "CONFIRMED"}}))
    assert by_key(owner_engine, event_id)[RIGHTS]["evidence"] == AC07_EVIDENCE


def test_confirmed_with_null_on_a_blank_field_stores_null(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    apply(app_engine, event_id, ac07(**{HEALTH: confirmed()}))
    assert by_key(owner_engine, event_id)[HEALTH] == field("CONFIRMED", None, None, "rev-1", NOW)


def test_not_applicable_stores_the_given_text_or_null(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    changes = {RIGHTS: not_applicable("no natural persons affected"), HEALTH: not_applicable("")}
    assert apply(app_engine, event_id, ac07(**changes)) == list(HARM_FIELDS)
    stored = by_key(owner_engine, event_id)
    assert stored[RIGHTS] == field(
        "NOT_APPLICABLE", "no natural persons affected", AC07_EVIDENCE, "rev-1", NOW
    )
    assert stored[HEALTH] == field("NOT_APPLICABLE", "", None, "rev-1", NOW)
    # NOT_APPLICABLE with null clears the text; it never falls back to the pre-fill.
    assert apply(app_engine, event_id, ac07(**{RIGHTS: not_applicable()}), now=LATER) == [
        HEALTH,
        RIGHTS,
    ]
    stored = by_key(owner_engine, event_id)
    assert stored[RIGHTS] == field("NOT_APPLICABLE", None, AC07_EVIDENCE, "rev-1", LATER)
    assert stored[HEALTH] == field("NOT_APPLICABLE", None, None, "rev-1", LATER)


def test_a_revision_touching_one_field_changes_only_its_confirmer_and_time(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    apply(app_engine, event_id, ac07())
    changes = {PROPERTY: confirmed("river contamination reported")}
    assert apply(app_engine, event_id, ac07(**changes), principal=REV_2, now=LATER) == [PROPERTY]
    assert by_key(owner_engine, event_id) == {
        HEALTH: field("NOT_APPLICABLE", None, None, "rev-1", NOW),
        INFRASTRUCTURE: field("NOT_APPLICABLE", None, None, "rev-1", NOW),
        RIGHTS: field("CONFIRMED", AC07_EVIDENCE, AC07_EVIDENCE, "rev-1", NOW),
        PROPERTY: field("CONFIRMED", "river contamination reported", None, "rev-2", LATER),
    }


def test_an_identical_call_changes_nothing(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    apply(app_engine, event_id, ac07())
    before = by_key(owner_engine, event_id)
    assert apply(app_engine, event_id, ac07(), principal=REV_2, now=LATER) == []
    # The pre-fill sent back as text is the same stored evidence, so no change either.
    assert (
        apply(app_engine, event_id, ac07(**{RIGHTS: confirmed(AC07_EVIDENCE)}), REV_2, LATER) == []
    )
    assert by_key(owner_engine, event_id) == before


def test_the_four_harm_field_rows_stay_locked_until_commit(
    app_engine: Engine, owner_engine: Engine, event_id: str
) -> None:
    apply(app_engine, event_id, ac07())
    decisions = require_complete(parse_harm_fields(ac07()))
    with Session(app_engine) as session:
        session.execute(
            text("SELECT event_id FROM event WHERE event_id = :event_id FOR UPDATE"),
            {"event_id": event_id},
        )
        # An identical call writes nothing but still holds the rows.
        assert apply_harm_fields(session, event_id, REV_2, decisions, LATER) == []
        for key in HARM_FIELDS:
            with owner_engine.connect() as other:
                with pytest.raises(OperationalError) as excinfo:
                    other.execute(
                        text(
                            "SELECT 1 FROM harm_field WHERE event_id = :event_id"
                            " AND field_key = :key FOR UPDATE NOWAIT"
                        ),
                        {"event_id": event_id, "key": key},
                    )
                assert isinstance(excinfo.value.orig, psycopg.errors.LockNotAvailable)
        session.rollback()


def test_the_event_row_and_the_ledger_are_untouched(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    event_sql = "SELECT * FROM event WHERE event_id = :event_id"
    event_before = rows(owner_engine, event_sql, event_id=event_id)
    ledger_before = entry_types(owner_engine)
    apply(app_engine, event_id, ac07())
    apply(app_engine, event_id, ac07(**{RIGHTS: confirmed("text")}), REV_2, LATER)
    event_after = rows(owner_engine, event_sql, event_id=event_id)
    assert event_after == event_before
    assert event_after[0]["state"] == "FLAGGED"
    assert entry_types(owner_engine) == ledger_before


def test_apply_leaves_a_dispositioned_event_row_byte_identical(
    owner_engine: Engine, app_engine: Engine, event_id: str
) -> None:
    """apply_harm_fields writes no event row at all (spec 1.5; Addendum rule 4). The event has
    a state and a deadline that a stray write could reset, and its xmin (the row version
    PostgreSQL renews on every UPDATE, even one that stores the same values) must not move."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event SET state = 'DISPOSITIONED', deadline_utc = :deadline"
                " WHERE event_id = :event_id"
            ),
            {"deadline": DEADLINE, "event_id": event_id},
        )
    event_sql = "SELECT *, xmin::text AS row_version FROM event WHERE event_id = :event_id"
    event_before = rows(owner_engine, event_sql, event_id=event_id)
    assert (event_before[0]["state"], event_before[0]["deadline_utc"]) == (
        "DISPOSITIONED",
        DEADLINE,
    )
    ledger_before = len(entry_types(owner_engine))
    changes = ac07(**{RIGHTS: confirmed("new reviewer text")})
    assert apply(app_engine, event_id, changes) == list(HARM_FIELDS)
    assert rows(owner_engine, event_sql, event_id=event_id) == event_before
    assert len(entry_types(owner_engine)) == ledger_before
    assert by_key(owner_engine, event_id)[RIGHTS] == field(
        "CONFIRMED", "new reviewer text", AC07_EVIDENCE, "rev-1", NOW
    )


@pytest.mark.parametrize("evidence", ACCEPTED_EVIDENCE.values(), ids=ACCEPTED_EVIDENCE.keys())
def test_each_accepted_evidence_is_stored_and_read_back(
    client: ApiClient, owner_engine: Engine, app_engine: Engine, event_id: str, evidence: str
) -> None:
    """Every evidence the parser accepts is stored by PostgreSQL exactly as given and shown
    unchanged by the detail (spec 1.5): nothing accepted may fail later as a 500 at the database
    (the reason of decision 154)."""
    apply(
        app_engine,
        event_id,
        ac07(**{RIGHTS: confirmed(evidence), HEALTH: not_applicable(evidence)}),
    )
    stored = by_key(owner_engine, event_id)
    assert (stored[RIGHTS]["status"], stored[RIGHTS]["evidence"]) == ("CONFIRMED", evidence)
    assert (stored[HEALTH]["status"], stored[HEALTH]["evidence"]) == ("NOT_APPLICABLE", evidence)
    shown = get(client, f"/v1/events/{event_id}").json()["harm_fields"]
    assert shown[RIGHTS]["evidence"] == evidence
    assert shown[HEALTH]["evidence"] == evidence


def test_one_info_line_per_changed_field_and_never_the_evidence(
    app_engine: Engine, event_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="api")
    override = "OVERRIDE-TEXT-7f3a"
    seen: list[str] = []

    def review_lines() -> list[tuple[int, str]]:
        seen.extend(record.getMessage() for record in caplog.records)
        lines = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "api.review"]
        caplog.clear()
        return lines

    def changed(key: str, old: str, new: str) -> tuple[int, str]:
        return (
            logging.INFO,
            f"harm field changed: event_id={event_id} field_key={key}"
            f" old_status={old} new_status={new}",
        )

    pending = "PREFILLED_PENDING_CONFIRMATION"
    apply(app_engine, event_id, ac07(**{RIGHTS: confirmed(override), HEALTH: confirmed()}))
    assert review_lines() == [
        changed(HEALTH, pending, "CONFIRMED"),
        changed(INFRASTRUCTURE, pending, "NOT_APPLICABLE"),
        changed(RIGHTS, pending, "CONFIRMED"),
        changed(PROPERTY, pending, "NOT_APPLICABLE"),
    ]
    # The override replaced by the pre-fill: a change with the same status.
    apply(app_engine, event_id, ac07(), REV_2, LATER)
    assert review_lines() == [
        changed(HEALTH, "CONFIRMED", "NOT_APPLICABLE"),
        changed(RIGHTS, "CONFIRMED", "CONFIRMED"),
    ]
    apply(app_engine, event_id, ac07(), REV_2, LATER)
    assert review_lines() == []
    assert seen
    for message in seen:
        for secret in (override, AC07_EVIDENCE, "sys-42 makes automated decisions"):
            assert secret not in message
