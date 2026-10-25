"""Phase 4 Part 1's schema: migration 0005 (Phase 4 spec 1.7 and 4.4, Addendum rule 5).

- harm_context: primary key (system_id, field_key); field_key is one of the four harm-field keys;
  system_id is a plain foreign key to monitored_system (no ON DELETE CASCADE, Addendum rule 5);
  context_text is required. The application role may only read it (spec 1.7: changed only by
  migration or seed).
- harm_field.prefill_evidence: a new nullable text column; rows that exist before 0005 get null.

Column names and types are checked in tests/worker/test_schema.py (the Part 1 slot), the grants
there through GRANT_EXCEPTIONS.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from psycopg import errors as pg_errors
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import get_engine
from tests.conftest import alembic_config
from tests.db_helpers import HARM_FIELDS, insert_event
from tests.part1.conftest import T0, rows
from tools.seed_part1 import HARM_CONTEXT

RIGHTS_TEXT = (
    "sys-42 makes automated decisions about natural persons, so degraded performance can"
    " infringe their fundamental rights (Art. 3(49)(c))."
)
INSERT_CONTEXT = (
    "INSERT INTO harm_context (system_id, field_key, context_text)"
    " VALUES (:system_id, :field_key, :context_text)"
)


def context(**changes: str | None) -> dict[str, str | None]:
    return {
        "system_id": "sys-42",
        "field_key": "health_safety_harm",
        "context_text": "sys-42 informs triage, so a wrong output can delay treatment.",
        **changes,
    }


@pytest.fixture
def system_only(seeded: None, owner_engine: Engine) -> None:
    """The seeded sys-42 without its harm_context row, for tests that write their own rows."""
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM harm_context"))


def test_seeding_writes_the_pinned_rights_row_for_sys_42(
    seeded: None, owner_engine: Engine
) -> None:
    """Phase 4 spec 4.4: one row, the rights field of sys-42; the other three fields have none."""
    assert rows(owner_engine, "SELECT * FROM harm_context") == [
        {
            "system_id": "sys-42",
            "field_key": "fundamental_rights_infringement",
            "context_text": RIGHTS_TEXT,
        }
    ]
    # The seed's own constant is the spec text, written out above rather than read from it.
    assert HARM_CONTEXT == ("fundamental_rights_infringement", RIGHTS_TEXT)


@pytest.mark.usefixtures("system_only")
def test_each_of_the_four_keys_is_accepted_once_per_system(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(INSERT_CONTEXT), [context(field_key=key) for key in HARM_FIELDS])
    assert sorted(
        r["field_key"] for r in rows(owner_engine, "SELECT field_key FROM harm_context")
    ) == sorted(HARM_FIELDS)
    with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text(INSERT_CONTEXT), context(context_text="a second row for the same key"))
    assert isinstance(excinfo.value.orig, pg_errors.UniqueViolation)


@pytest.mark.usefixtures("system_only")
@pytest.mark.parametrize("field_key", ["fifth_harm", "Health_Safety_Harm", ""])
def test_a_fifth_field_key_is_rejected(owner_engine: Engine, field_key: str) -> None:
    with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text(INSERT_CONTEXT), context(field_key=field_key))
    assert isinstance(excinfo.value.orig, pg_errors.CheckViolation)
    assert "ck_harm_context_field_key" in str(excinfo.value.orig)


@pytest.mark.usefixtures("system_only")
def test_context_text_is_required(owner_engine: Engine) -> None:
    with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text(INSERT_CONTEXT), context(context_text=None))
    assert isinstance(excinfo.value.orig, pg_errors.NotNullViolation)


@pytest.mark.usefixtures("system_only")
def test_an_unknown_system_id_is_a_foreign_key_violation(owner_engine: Engine) -> None:
    with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text(INSERT_CONTEXT), context(system_id="sys-404"))
    assert isinstance(excinfo.value.orig, pg_errors.ForeignKeyViolation)
    assert "fk_harm_context_system" in str(excinfo.value.orig)


def test_a_system_with_context_cannot_be_deleted(seeded: None, owner_engine: Engine) -> None:
    """Addendum rule 5: a plain foreign key, not ON DELETE CASCADE."""
    with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM monitored_system WHERE system_id = 'sys-42'"))
    assert isinstance(excinfo.value.orig, pg_errors.ForeignKeyViolation)
    assert "fk_harm_context_system" in str(excinfo.value.orig)
    assert len(rows(owner_engine, "SELECT 1 FROM harm_context")) == 1
    assert len(rows(owner_engine, "SELECT 1 FROM monitored_system")) == 1


def test_the_application_role_can_read_harm_context(seeded: None, app_engine: Engine) -> None:
    assert rows(app_engine, "SELECT field_key FROM harm_context") == [
        {"field_key": "fundamental_rights_infringement"}
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO harm_context (system_id, field_key, context_text)"
        " VALUES ('sys-42', 'health_safety_harm', 'written by the application')",
        "UPDATE harm_context SET context_text = 'changed by the application'",
        "DELETE FROM harm_context",
    ],
)
def test_the_application_role_cannot_write_harm_context(
    seeded: None, app_engine: Engine, owner_engine: Engine, statement: str
) -> None:
    """Phase 4 spec 1.7: harm_context changes only through migration or seed."""
    with pytest.raises(DBAPIError) as excinfo, app_engine.begin() as conn:
        conn.execute(text(statement))
    assert isinstance(excinfo.value.orig, pg_errors.InsufficientPrivilege)
    assert rows(owner_engine, "SELECT context_text FROM harm_context") == [
        {"context_text": RIGHTS_TEXT}
    ]


def test_prefill_evidence_is_nullable_text_and_the_application_role_can_write_it(
    app_engine: Engine,
) -> None:
    """The application role keeps normal read and write on harm_field (spec 1.7)."""
    with app_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="threshold_breach", detected_at=T0)
        conn.execute(
            text(
                "UPDATE harm_field SET prefill_evidence = 'pre-filled'"
                " WHERE event_id = 'evt-1' AND field_key = 'fundamental_rights_infringement'"
            )
        )
    assert rows(
        app_engine,
        "SELECT field_key, prefill_evidence FROM harm_field WHERE event_id = 'evt-1'"
        " ORDER BY field_key",
    ) == [
        {"field_key": key, "prefill_evidence": "pre-filled" if key == HARM_FIELDS[2] else None}
        for key in sorted(HARM_FIELDS)
    ]


@pytest.fixture
def at_0004(owner_engine: Engine) -> Iterator[None]:
    """Move the database back to 0004 for one test, and always return it to head."""
    get_engine().dispose()
    command.downgrade(alembic_config(), "0004")
    try:
        yield
    finally:
        command.upgrade(alembic_config(), "head")


@pytest.mark.usefixtures("at_0004")
def test_harm_field_rows_from_before_0005_get_null_prefill_evidence(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('harm_context')")).scalar() is None
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="threshold_breach", detected_at=T0)
        insert_event(conn, "evt-2", event_type="monitoring_gap", detected_at=T0)
    command.upgrade(alembic_config(), "0005")
    assert rows(
        owner_engine, "SELECT count(*) AS n, count(prefill_evidence) AS filled FROM harm_field"
    ) == [{"n": 8, "filled": 0}]


def test_the_chain_runs_0004_then_0005_then_0006() -> None:
    """The placeholder chain of Addendum 9.1 is unchanged by filling 0005."""
    script = ScriptDirectory.from_config(alembic_config())
    review = script.get_revision("0005")
    assert review is not None
    assert review.down_revision == "0004"
    dispositions = script.get_revision("0006")
    assert dispositions is not None
    assert dispositions.down_revision == "0005"
    assert script.get_heads() == ["0007"]
