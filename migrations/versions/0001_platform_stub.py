"""STUB for Parts 1 and 4: platform tables and database roles.

Part 4 owns migration 0001 (clock_state, api_key, monitored_system, threshold_version,
sweeper_heartbeat, database roles). Until Part 4 lands, the roles are created by
tests/sql/bootstrap_roles.sql, and this revision creates only the two tables Part 3 reads and
writes, with the columns Part 3 uses:

- monitored_system (Part 4): one row per monitored system. Part 1's ingest sets
  last_received_at; the gap sweeper reads it, and falls back to monitoring_started_at for a
  system that has never sent a sample.
- event (Part 1): the event table. The gap sweeper creates monitoring_gap events in it through
  app/events.py, which Part 1 replaces along with this table.

Revision ID: 0001
Revises:
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
STUB_TABLES = ("monitored_system", "event")


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
        sa.Column("monitoring_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "event",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_name", sa.Text, nullable=True),
        sa.Column("observed_value", sa.Text, nullable=True),
        sa.Column("threshold_value", sa.Text, nullable=True),
        sa.Column("harm_fields", JSONB, nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for table in STUB_TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    # IF EXISTS: a database upgraded by the earlier, empty version of this stub has neither.
    for table in reversed(STUB_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
