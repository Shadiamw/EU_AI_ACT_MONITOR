"""Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.

Part 2. The application role (the user in DATABASE_URL) gets INSERT and SELECT only.
The trigger stays enabled in production; tamper tests disable it as the owner role.
There is deliberately no TRUNCATE trigger: test reset truncates the ledger as the owner.

Revision ID: 0002
Revises: 0001
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_NAME = "ledger_entry_block_update_delete"
TRIGGER_FUNCTION = "ledger_entry_block_update_delete_fn"

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_HEX64 = "^[0-9a-f]{64}$"
_RECORDED_AT = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"


def _app_role() -> str:
    """The application role is the user in DATABASE_URL, so grants follow Part 4's naming."""
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    owner = op.get_bind().execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("DATABASE_URL must use the application role, not the owner role")
    return role


def upgrade() -> None:
    app_role = _app_role()

    op.create_table(
        "ledger_entry",
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("entry_type", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("subject_id", sa.Text, nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("recorded_at", sa.Text, nullable=False),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.UniqueConstraint("prev_hash", name="uq_ledger_entry_prev_hash"),
        sa.CheckConstraint("seq >= 1", name="ck_ledger_entry_seq_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_ledger_entry_payload_object"
        ),
        sa.CheckConstraint(f"recorded_at ~ '{_RECORDED_AT}'", name="ck_ledger_entry_recorded_at"),
        sa.CheckConstraint(f"prev_hash ~ '{_HEX64}'", name="ck_ledger_entry_prev_hash_hex"),
        sa.CheckConstraint(f"hash ~ '{_HEX64}'", name="ck_ledger_entry_hash_hex"),
    )
    op.create_index("ix_ledger_entry_subject_seq", "ledger_entry", ["subject_id", "seq"])

    op.execute(
        f"""
        CREATE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entry is append-only: % is not allowed', TG_OP;
        END;
        $$
        """
    )
    # Statement-level, so an UPDATE or DELETE fails even when it matches no rows.
    op.execute(
        f"""
        CREATE TRIGGER {TRIGGER_NAME}
        BEFORE UPDATE OR DELETE ON ledger_entry
        FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER_FUNCTION}()
        """
    )

    op.execute("REVOKE ALL ON TABLE ledger_entry FROM PUBLIC")
    op.execute(f'REVOKE ALL ON TABLE ledger_entry FROM "{app_role}"')
    op.execute(f'GRANT SELECT, INSERT ON TABLE ledger_entry TO "{app_role}"')


def downgrade() -> None:
    op.drop_table("ledger_entry")
    op.execute(f"DROP FUNCTION {TRIGGER_FUNCTION}()")
