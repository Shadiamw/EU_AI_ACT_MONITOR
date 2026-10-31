"""Phase 4 Part 2: migration 0006, the disposition table (spec 2.9). The column list and the
application role's grants (INSERT and SELECT only) are checked by the Part 2 slot and the
GRANT_EXCEPTIONS entry in tests/worker/test_schema.py.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.db_helpers import insert_event
from tests.part1.conftest import rows

T = datetime(2026, 9, 27, 10, 12, tzinfo=UTC)
INSERT = text(
    "INSERT INTO disposition (disposition_id, event_id, seq_in_event, tier, rationale,"
    " reviewer_id, assigned_at, anchor_at, calendar, deadline_utc, supersedes_id) VALUES"
    " (:disposition_id, :event_id, :seq_in_event, :tier, :rationale, 'rev-1', :t, :t,"
    " 'calendar_days', :deadline_utc, :supersedes_id)"
)


def row(**changes: Any) -> dict[str, Any]:
    return {
        "disposition_id": "dsp-1",
        "event_id": "evt-1",
        "seq_in_event": 1,
        "tier": "DEATH",
        "rationale": None,
        "t": T,
        "deadline_utc": T + timedelta(days=10),
        "supersedes_id": None,
        **changes,
    }


@pytest.fixture
def event(owner_engine: Engine) -> str:
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="threshold_breach", detected_at=T)
        conn.execute(INSERT, row())
    return "evt-1"


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"disposition_id": "dsp-2", "event_id": "evt-missing"}, "fk_disposition_event"),
        ({"disposition_id": "dsp-2", "tier": "SERIOUS"}, "ck_disposition_tier"),
        ({"disposition_id": "dsp-2"}, "uq_disposition_event_seq"),
        ({"disposition_id": "dsp-2", "seq_in_event": 0}, "ck_disposition_seq_positive"),
        # A first row never supersedes; a later row always does, and only an existing row.
        ({"disposition_id": "dsp-2", "seq_in_event": 2}, "ck_disposition_supersedes"),
        (
            {"disposition_id": "dsp-2", "seq_in_event": 2, "supersedes_id": "dsp-9"},
            "fk_disposition_supersedes",
        ),
        # NOT_SERIOUS has no deadline and a rationale; a serious tier has a deadline (INV-2).
        (
            {
                "disposition_id": "dsp-2",
                "seq_in_event": 2,
                "supersedes_id": "dsp-1",
                "tier": "NOT_SERIOUS",
                "rationale": "x",
            },
            "ck_disposition_deadline",
        ),
        (
            {
                "disposition_id": "dsp-2",
                "seq_in_event": 2,
                "supersedes_id": "dsp-1",
                "deadline_utc": None,
            },
            "ck_disposition_deadline",
        ),
        (
            {
                "disposition_id": "dsp-2",
                "seq_in_event": 2,
                "supersedes_id": "dsp-1",
                "tier": "NOT_SERIOUS",
                "deadline_utc": None,
            },
            "ck_disposition_not_serious_rationale",
        ),
        (
            {
                "disposition_id": "dsp-2",
                "seq_in_event": 2,
                "supersedes_id": "dsp-1",
                "rationale": "",
            },
            "ck_disposition_rationale_not_empty",
        ),
    ],
)
def test_constraints(
    owner_engine: Engine, event: str, changes: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), owner_engine.begin() as conn:
        conn.execute(INSERT, row(**changes))


def test_history_cannot_fork(owner_engine: Engine, event: str) -> None:
    """supersedes_id is unique: two rows cannot revise the same row (spec 2.9)."""
    second = {"seq_in_event": 2, "supersedes_id": "dsp-1"}
    with owner_engine.begin() as conn:
        conn.execute(INSERT, row(disposition_id="dsp-2", **second))
    third = row(disposition_id="dsp-3", seq_in_event=3, supersedes_id="dsp-1")
    with (
        pytest.raises(IntegrityError, match="uq_disposition_supersedes"),
        owner_engine.begin() as c,
    ):
        c.execute(INSERT, third)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE disposition SET tier = 'OTHER_SERIOUS'",
        "UPDATE disposition SET tier = 'OTHER_SERIOUS' WHERE false",
        "DELETE FROM disposition",
        "DELETE FROM disposition WHERE false",
    ],
)
@pytest.mark.parametrize("role", ["app", "owner"])
def test_the_table_is_append_only(
    owner_engine: Engine, app_engine: Engine, event: str, statement: str, role: str
) -> None:
    """INV-4: the application role has no UPDATE or DELETE privilege, and the statement-level
    trigger refuses both even for the owner and even when nothing matches. Only the owner's
    TRUNCATE (test reset) empties it."""
    engine = app_engine if role == "app" else owner_engine
    match = "permission denied" if role == "app" else "disposition is append-only"
    with pytest.raises(DBAPIError, match=match), engine.begin() as conn:
        conn.execute(text(statement))
    assert len(rows(owner_engine, "SELECT * FROM disposition")) == 1


def test_the_application_role_inserts_and_selects_but_cannot_truncate(
    app_engine: Engine, event: str
) -> None:
    with app_engine.begin() as conn:
        conn.execute(INSERT, row(disposition_id="dsp-2", seq_in_event=2, supersedes_id="dsp-1"))
        assert conn.execute(text("SELECT count(*) FROM disposition")).scalar_one() == 2
    with pytest.raises(DBAPIError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(text("TRUNCATE disposition"))


def test_the_trigger_is_statement_level_before_update_or_delete(owner_engine: Engine) -> None:
    [trigger] = rows(
        owner_engine,
        "SELECT action_timing, action_orientation, string_agg(event_manipulation, ','"
        " ORDER BY event_manipulation) AS events FROM information_schema.triggers"
        " WHERE event_object_table = 'disposition' GROUP BY action_timing, action_orientation",
    )
    assert trigger == {
        "action_timing": "BEFORE",
        "action_orientation": "STATEMENT",
        "events": "DELETE,UPDATE",
    }


def test_event_and_seq_are_indexed(owner_engine: Engine) -> None:
    [index] = rows(
        owner_engine,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_disposition_event_seq'",
    )
    assert "(event_id, seq_in_event)" in index["indexdef"]
