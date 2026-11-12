"""Ledger operations (Phase 5 spec 6.1 and 6.5): ledger_verification_run and platform_counter.

Phase 5 Part 3. The revision id and the chain are fixed by the Phase 5 base commit (spec 11.1
step 2; Part 3 decision 294); Part 3 fills in upgrade() and downgrade() in this file only.

- ledger_verification_run (M4, spec 6.1): one row per verification run, insert-only. A
  statement-level trigger rejects every UPDATE and DELETE, as migration 0006 does for
  disposition; the application role gets INSERT and SELECT only. A unique index on the
  wall-clock UTC date of started_at, for NIGHTLY rows only, allows one nightly run per day
  (decision 302).
- platform_counter (M6, spec 6.5; decision 301): a single row holding reset_count, the
  number of POST /v1/test/reset calls, shown by /healthz. The singleton key and its check keep
  it to one row, as clock_state does in migration 0001. platform.reset() writes it as the owner
  (decision 300); the application role gets SELECT only. No row means 0.

Revision ID: 0010
Revises: 0009
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ledger_verification_run"
TRIGGER_FUNCTION = "ledger_verification_run_block_update_delete_fn"
TRIGGER_NAME = "ledger_verification_run_block_update_delete"
NIGHTLY_INDEX = "uq_ledger_verification_run_one_nightly_per_utc_date"
COUNTER_TABLE = "platform_counter"

TRIGGERS = ("NIGHTLY", "MANUAL")
HEAD_CHECKS = ("OK", "REGRESSED", "FIRST_RUN")

_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    owner = op.get_bind().execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("DATABASE_URL must use the application role, not the owner role")
    return role


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    app_role = _app_role()
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        TABLE,
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("started_at", timestamp, nullable=False),
        sa.Column("finished_at", timestamp, nullable=False),
        sa.Column("valid", sa.Boolean, nullable=False),
        sa.Column("entries_checked", sa.Integer, nullable=False),
        sa.Column("first_mismatch", sa.Text, nullable=True),
        sa.Column("head_seq", sa.BigInteger, nullable=True),
        sa.Column("head_hash", sa.Text, nullable=True),
        sa.Column("head_check", sa.Text, nullable=False),
        sa.Column("projection_mismatches", JSONB, nullable=False),
        sa.Column("report_mismatches", JSONB, nullable=False),
        sa.CheckConstraint(f"trigger IN ({_in(TRIGGERS)})", name="ck_lvr_trigger"),
        sa.CheckConstraint(f"head_check IN ({_in(HEAD_CHECKS)})", name="ck_lvr_head_check"),
        sa.CheckConstraint("(head_seq IS NULL) = (head_hash IS NULL)", name="ck_lvr_head"),
    )
    op.execute(
        f"CREATE UNIQUE INDEX {NIGHTLY_INDEX} ON {TABLE}"
        " (((started_at AT TIME ZONE 'UTC')::date)) WHERE trigger = 'NIGHTLY'"
    )

    op.execute(
        f"""
        CREATE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '{TABLE} is append-only: % is not allowed', TG_OP;
        END;
        $$
        """
    )
    # Statement-level, so an UPDATE or DELETE fails even when it matches no rows.
    op.execute(
        f"""
        CREATE TRIGGER {TRIGGER_NAME}
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER_FUNCTION}()
        """
    )

    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC")
    op.execute(f'REVOKE ALL ON TABLE {TABLE} FROM "{app_role}"')
    op.execute(f'GRANT SELECT, INSERT ON TABLE {TABLE} TO "{app_role}"')

    op.create_table(
        COUNTER_TABLE,
        sa.Column("singleton", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column("reset_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.CheckConstraint("singleton", name="ck_platform_counter_single_row"),
    )
    op.execute(f"REVOKE ALL ON TABLE {COUNTER_TABLE} FROM PUBLIC")
    op.execute(f'REVOKE ALL ON TABLE {COUNTER_TABLE} FROM "{app_role}"')
    op.execute(f'GRANT SELECT ON TABLE {COUNTER_TABLE} TO "{app_role}"')


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {COUNTER_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FUNCTION}()")
