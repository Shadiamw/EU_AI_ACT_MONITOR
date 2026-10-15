"""STUB for Part 4: platform tables.

Part 4 owns migration 0001 (spec 4.4: clock_state, api_key, monitored_system, threshold_version,
sweeper_heartbeat, database roles). Until Part 4's real 0001 replaces this file, it creates only
the two Part 4 tables that Parts 2 and 3 use, with the spec 4.4 names and columns:

- monitored_system: one row per monitored system. Part 4's seed and reset insert it (sys-42:
  sampling_interval_s 300, gap_timeout_intervals 3, last_received_at = seed time). Part 1's
  ingest updates last_received_at; the gap sweeper reads it.
- sweeper_heartbeat: a single row holding the real (wall-clock) time of the last sweep. The
  worker writes it; Part 4's health endpoint reads it. The boolean key `singleton` keeps it to
  one row (the spec names only last_ran_at; decision 95).

The database roles are created by tests/sql/bootstrap_roles.sql until Part 4 lands. The
application role (the user in DATABASE_URL) gets normal read and write on these tables (spec
4.4: everything except the ledger).

Revision ID: 0001
Revises:
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = ("monitored_system", "sweeper_heartbeat")


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    return role


def upgrade() -> None:
    app_role = _app_role()
    op.create_table(
        "monitored_system",
        sa.Column("system_id", sa.Text, primary_key=True),
        sa.Column("sampling_interval_s", sa.Integer, nullable=False),
        sa.Column("gap_timeout_intervals", sa.Integer, nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sweeper_heartbeat",
        sa.Column("singleton", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column("last_ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("singleton", name="ck_sweeper_heartbeat_single_row"),
    )
    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    # IF EXISTS: a database upgraded by an earlier version of this stub may lack them.
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
