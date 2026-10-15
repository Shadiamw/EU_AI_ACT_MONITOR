"""M0: migration 0002, table grants and the UPDATE/DELETE-blocking trigger."""

import pytest
from alembic import command
from psycopg import errors as pg_errors
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import alembic_config
from tests.db_helpers import TRIGGER_NAME, insert_raw_entry, trigger_disabled

APP_ROLE = "monitor_app"


def test_migration_downgrades_and_upgrades(owner_engine: Engine) -> None:
    command.downgrade(alembic_config(), "0001")
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('ledger_entry')")).scalar() is None
        assert (
            conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = :name"),
                {"name": f"{TRIGGER_NAME}_fn"},
            ).scalar_one()
            == 0
        )
    command.upgrade(alembic_config(), "head")
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('ledger_entry')")).scalar() is not None


@pytest.mark.parametrize(
    ("privilege", "expected"),
    [
        ("SELECT", True),
        ("INSERT", True),
        ("UPDATE", False),
        ("DELETE", False),
        ("TRUNCATE", False),
        ("REFERENCES", False),
        ("TRIGGER", False),
    ],
)
def test_app_role_has_insert_and_select_only(
    owner_engine: Engine, privilege: str, expected: bool
) -> None:
    with owner_engine.connect() as conn:
        granted = conn.execute(
            text("SELECT has_table_privilege(:role, 'ledger_entry', :privilege)"),
            {"role": APP_ROLE, "privilege": privilege},
        ).scalar_one()
    assert granted is expected


def test_public_has_no_privileges(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        acl = conn.execute(
            text(
                "SELECT grantee, privilege_type FROM information_schema.role_table_grants"
                " WHERE table_name = 'ledger_entry' AND grantee NOT IN (:owner, :app)"
            ),
            {"owner": "monitor_owner", "app": APP_ROLE},
        ).all()
    assert acl == []


def test_app_role_can_insert_and_select(app_engine: Engine) -> None:
    with app_engine.begin() as conn:
        insert_raw_entry(conn, 1, payload='{"systems": ["sys-42"]}')
    with app_engine.connect() as conn:
        row = conn.execute(text("SELECT seq, payload FROM ledger_entry")).one()
    assert row.seq == 1
    assert row.payload == {"systems": ["sys-42"]}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entry SET actor_id = 'intruder'",
        "DELETE FROM ledger_entry",
        "TRUNCATE ledger_entry",
    ],
)
def test_app_role_cannot_update_delete_or_truncate(app_engine: Engine, statement: str) -> None:
    """Spec test 'Application role cannot update' (plus TRUNCATE, which the spec revokes)."""
    with app_engine.begin() as conn:
        insert_raw_entry(conn, 1)
    with pytest.raises(DBAPIError) as excinfo, app_engine.begin() as conn:
        conn.execute(text(statement))
    assert isinstance(excinfo.value.orig, pg_errors.InsufficientPrivilege)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entry SET payload = '{\"edited\": true}'::jsonb WHERE seq = 1",
        "DELETE FROM ledger_entry WHERE seq = 1",
        "UPDATE ledger_entry SET actor_id = 'x' WHERE false",
        "DELETE FROM ledger_entry WHERE false",
    ],
)
def test_owner_update_and_delete_fail_while_trigger_enabled(
    owner_engine: Engine, statement: str
) -> None:
    """Addendum rule 1: with the trigger enabled, UPDATE and DELETE as the owner both fail."""
    with owner_engine.begin() as conn:
        insert_raw_entry(conn, 1)
    with pytest.raises(DBAPIError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text(statement))
    assert isinstance(excinfo.value.orig, pg_errors.RaiseException)
    assert "ledger_entry is append-only" in str(excinfo.value.orig)
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT actor_id FROM ledger_entry")).scalar_one() == "system"


def test_trigger_definition_blocks_update_and_delete_but_not_truncate(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as conn:
        triggers = conn.execute(
            text(
                "SELECT tgname, tgenabled, pg_get_triggerdef(oid) AS definition FROM pg_trigger"
                " WHERE tgrelid = 'ledger_entry'::regclass AND NOT tgisinternal"
            )
        ).all()
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.tgname == TRIGGER_NAME
    assert trigger.tgenabled == "O"  # enabled in normal operation
    assert "BEFORE DELETE OR UPDATE ON public.ledger_entry FOR EACH STATEMENT" in (
        trigger.definition
    )
    assert "TRUNCATE" not in trigger.definition


def test_owner_can_truncate(owner_engine: Engine) -> None:
    """Addendum rule 1: no TRUNCATE trigger, because test reset truncates as the owner."""
    with owner_engine.begin() as conn:
        insert_raw_entry(conn, 1)
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entry"))
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one() == 0


def test_owner_can_edit_with_trigger_disabled_and_trigger_is_restored(
    owner_engine: Engine,
) -> None:
    """The tamper-fixture procedure from addendum rule 1 works and leaves the trigger on."""
    with owner_engine.begin() as conn:
        insert_raw_entry(conn, 1, payload='{"value": "0.34"}')
    with trigger_disabled(owner_engine) as conn:
        conn.execute(
            text("""UPDATE ledger_entry SET payload = '{"value": "0.35"}' WHERE seq = 1""")
        )
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT payload FROM ledger_entry")).scalar_one() == {
            "value": "0.35"
        }
        assert (
            conn.execute(
                text("SELECT tgenabled FROM pg_trigger WHERE tgname = :name"),
                {"name": TRIGGER_NAME},
            ).scalar_one()
            == "O"
        )
    with pytest.raises(DBAPIError), owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM ledger_entry"))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("seq", "0"),
        ("payload", "'[]'::jsonb"),
        ("payload", "'\"text\"'::jsonb"),
        ("recorded_at", "'2026-09-27T10:00:00Z'"),
        ("recorded_at", "'2026-09-27T10:00:00.000+00:00'"),
        ("prev_hash", "'ABC'"),
        ("hash", "upper(repeat('a', 64))"),
    ],
)
def test_constraints_reject_malformed_rows(owner_engine: Engine, column: str, value: str) -> None:
    values = {
        "seq": "1",
        "entry_type": "'SEED'",
        "actor_id": "'system'",
        "subject_id": "NULL",
        "payload": "'{}'::jsonb",
        "recorded_at": "'2026-09-27T10:00:00.000Z'",
        "prev_hash": "repeat('0', 64)",
        "hash": "repeat('a', 64)",
    }
    values[column] = value
    statement = (
        f"INSERT INTO ledger_entry ({', '.join(values)}) VALUES ({', '.join(values.values())})"
    )
    with pytest.raises(DBAPIError) as excinfo, owner_engine.begin() as conn:
        conn.execute(text(statement))
    assert isinstance(excinfo.value.orig, pg_errors.CheckViolation)


def test_prev_hash_is_unique(owner_engine: Engine) -> None:
    """Spec: unique constraint on prev_hash as an extra guard against forks."""
    with owner_engine.begin() as conn:
        insert_raw_entry(conn, 1)
    with pytest.raises(DBAPIError) as excinfo, owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ledger_entry"
                " (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)"
                " SELECT 2, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash,"
                " repeat('b', 64) FROM ledger_entry WHERE seq = 1"
            )
        )
    assert isinstance(excinfo.value.orig, pg_errors.UniqueViolation)
