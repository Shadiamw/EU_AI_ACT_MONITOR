"""Dispositions (Phase 4 spec 2.9 and 4.4): disposition, its grants and its UPDATE/DELETE-blocking
trigger.

Phase 4 Part 2. This file fills the placeholder of the Phase 4 base commit (Addendum 9.1); the
revision id and the chain are unchanged.

- disposition: one row per decision on an event, never edited (INV-4, REQ-09). A revision is a
  new row whose supersedes_id names the previous one; supersedes_id is unique, so the history
  cannot fork, and null only on the first row (seq_in_event = 1). anchor_at and calendar are
  the first row's, copied to every row. deadline_utc is the deadline the row put in force, null
  exactly for NOT_SERIOUS (INV-2), which also always carries a rationale. rationale is stored
  trimmed and null when blank, so it is never the empty string.
- event_id is a plain foreign key to event (Addendum rule 5); supersedes_id references the
  previous row of the same table (Part 2 decision 179). The unique (event_id, seq_in_event)
  constraint is the index on those columns that spec 2.9 asks for.
- Append-only: the application role gets INSERT and SELECT only, and a statement-level trigger
  raises on any UPDATE or DELETE, even one that matches no row, exactly like ledger_entry
  (0002). There is no TRUNCATE trigger: test reset truncates as the owner role.

PUBLIC gets nothing.

Revision ID: 0006
Revises: 0005
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_NAME = "disposition_block_update_delete"
TRIGGER_FUNCTION = "disposition_block_update_delete_fn"

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TIERS = ("DEATH", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS", "NOT_SERIOUS")


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    owner = op.get_bind().execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("DATABASE_URL must use the application role, not the owner role")
    return role


def upgrade() -> None:
    app_role = _app_role()
    tiers = ", ".join(f"'{tier}'" for tier in TIERS)
    op.create_table(
        "disposition",
        sa.Column("disposition_id", sa.Text, primary_key=True),
        sa.Column(
            "event_id",
            sa.Text,
            sa.ForeignKey("event.event_id", name="fk_disposition_event"),
            nullable=False,
        ),
        sa.Column("seq_in_event", sa.Integer, nullable=False),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("reviewer_id", sa.Text, nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calendar", sa.Text, nullable=False),
        sa.Column("deadline_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_id",
            sa.Text,
            sa.ForeignKey("disposition.disposition_id", name="fk_disposition_supersedes"),
            nullable=True,
        ),
        sa.UniqueConstraint("event_id", "seq_in_event", name="uq_disposition_event_seq"),
        sa.UniqueConstraint("supersedes_id", name="uq_disposition_supersedes"),
        sa.CheckConstraint("seq_in_event >= 1", name="ck_disposition_seq_positive"),
        sa.CheckConstraint(f"tier IN ({tiers})", name="ck_disposition_tier"),
        sa.CheckConstraint(
            "(seq_in_event = 1) = (supersedes_id IS NULL)", name="ck_disposition_supersedes"
        ),
        sa.CheckConstraint(
            "(tier = 'NOT_SERIOUS') = (deadline_utc IS NULL)", name="ck_disposition_deadline"
        ),
        sa.CheckConstraint(
            "tier <> 'NOT_SERIOUS' OR rationale IS NOT NULL",
            name="ck_disposition_not_serious_rationale",
        ),
        sa.CheckConstraint(
            "rationale IS NULL OR rationale <> ''", name="ck_disposition_rationale_not_empty"
        ),
    )

    op.execute(
        f"""
        CREATE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'disposition is append-only: % is not allowed', TG_OP;
        END;
        $$
        """
    )
    # Statement-level, so an UPDATE or DELETE fails even when it matches no rows.
    op.execute(
        f"""
        CREATE TRIGGER {TRIGGER_NAME}
        BEFORE UPDATE OR DELETE ON disposition
        FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER_FUNCTION}()
        """
    )

    op.execute("REVOKE ALL ON TABLE disposition FROM PUBLIC")
    op.execute(f'REVOKE ALL ON TABLE disposition FROM "{app_role}"')
    op.execute(f'GRANT SELECT, INSERT ON TABLE disposition TO "{app_role}"')


def downgrade() -> None:
    # IF EXISTS: a database upgraded while 0006 was still the base commit's empty placeholder is
    # at 0006 or later with no table (Part 2 decision 190).
    op.execute("DROP TABLE IF EXISTS disposition")
    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FUNCTION}()")
