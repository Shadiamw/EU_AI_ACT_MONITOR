"""Personal data (Phase 5 spec 4.3): record_key and event.erased_at.

Phase 5 Part 2 (merge 2b).

- record_key: one random 256-bit data key per event, created on the first seal. Every personal
  value written to an append-only store (the disposition rationale, the ledger payload that
  carries it, the report bytes) is sealed under it. The key is stored unwrapped (D7): this
  design gives erasure, not confidentiality. Erasure sets data_key to NULL and destroyed_at to
  the erasure time; the row stays as a tombstone, so "erased" remains provable. The check keeps
  the two columns consistent: a key is present exactly when it has not been destroyed.
- event.erased_at: set by erasure, read by the report routes and events.detail.
- The application role gets SELECT, INSERT and UPDATE on record_key and no DELETE. PUBLIC gets
  nothing. event keeps the grants of 0003.

Revision ID: 0009
Revises: 0008
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


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
    op.create_table(
        "record_key",
        sa.Column("subject_id", sa.Text, primary_key=True),
        sa.Column("data_key", sa.LargeBinary, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(data_key IS NULL) = (destroyed_at IS NOT NULL)", name="ck_record_key_destroyed"
        ),
        sa.CheckConstraint(
            "data_key IS NULL OR octet_length(data_key) = 32", name="ck_record_key_length"
        ),
    )
    op.execute("REVOKE ALL ON TABLE record_key FROM PUBLIC")
    op.execute(f'REVOKE ALL ON TABLE record_key FROM "{app_role}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON TABLE record_key TO "{app_role}"')
    op.add_column("event", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("event", "erased_at")
    op.execute("DROP TABLE IF EXISTS record_key")
