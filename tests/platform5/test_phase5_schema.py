"""Phase 5 spec 11.1 step 2: the Phase 5 tables of migrations 0008 to 0010 exist, are in the
shared truncate list in the spec's order, are emptied by reset, and carry the application-role
privileges their owners pinned (spec 3.5, 4.3, 6.1, 6.5). Columns are checked per part in
tests/worker/test_schema.py; this test checks what Part 4's base commit promised."""

from sqlalchemy import Engine, text

from app import platform
from tests.part1.conftest import rows

PHASE5 = (
    "report",
    "report_submission",
    "record_key",
    "ledger_verification_run",
    "platform_counter",
)

# Spec 3.5 and 6.1: insert-only; 4.3: a tombstone (no DELETE); 6.5: only reset writes it.
APP_PRIVILEGES = {
    "report": ["INSERT", "SELECT"],
    "report_submission": ["INSERT", "SELECT"],
    "record_key": ["INSERT", "SELECT", "UPDATE"],
    "ledger_verification_run": ["INSERT", "SELECT"],
    "platform_counter": ["SELECT"],
}


def test_phase5_tables_and_grants(owner_engine: Engine) -> None:
    assert platform.PHASE5_TABLES == PHASE5

    with owner_engine.connect() as conn:
        listed = platform.with_phase4_tables(conn, platform.RESET_TABLES)
    assert listed[-len(PHASE5) :] == PHASE5, "every Phase 5 table is in the reset truncate list"

    [head] = rows(owner_engine, "SELECT version_num FROM alembic_version")
    assert head == {"version_num": "0010"}

    for table, expected in APP_PRIVILEGES.items():
        got = sorted(
            r["privilege_type"]
            for r in rows(
                owner_engine,
                "SELECT privilege_type FROM information_schema.role_table_grants"
                " WHERE table_name = :t AND grantee = 'monitor_app'",
                t=table,
            )
        )
        assert got == expected, table
        public = rows(
            owner_engine,
            "SELECT 1 FROM information_schema.role_table_grants"
            " WHERE table_name = :t AND grantee = 'PUBLIC'",
            t=table,
        )
        assert public == [], table


def test_reset_empties_the_phase5_tables_and_keeps_counting_resets(
    seeded: None, owner_engine: Engine
) -> None:
    platform.reset()
    [before] = rows(owner_engine, "SELECT reset_count FROM platform_counter")
    platform.reset()
    for table in ("report", "report_submission", "record_key", "ledger_verification_run"):
        with owner_engine.connect() as conn:
            count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, table
    [after] = rows(owner_engine, "SELECT reset_count FROM platform_counter")
    assert after["reset_count"] == before["reset_count"] + 1
