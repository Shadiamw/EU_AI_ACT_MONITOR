"""STUB for Part 4: platform tables.

Part 4 owns migration 0001 (spec 4.4: clock_state, api_key, monitored_system, threshold_version,
sweeper_heartbeat, database roles). Until Part 4's real 0001 replaces this file, it creates only
the four Part 4 tables that Parts 1, 2 and 3 use, with the spec 4.4 names and columns:

- monitored_system: one row per monitored system. Part 4's seed and reset insert it (sys-42:
  sampling_interval_s 300, gap_timeout_intervals 3, last_received_at = seed time). Part 1's
  ingest updates last_received_at; the gap sweeper reads it.
- sweeper_heartbeat: a single row holding the real (wall-clock) time of the last sweep. The
  worker writes it; Part 4's health endpoint reads it. The boolean key `singleton` keeps it to
  one row (the spec names only last_ran_at; decision 95).
- api_key: one row per seeded credential, holding only the SHA-256 hex digest of the key (spec
  1.3, 4.4). Part 1's auth reads it.
- threshold_version: the versioned threshold limits Part 1's ingest compares against (spec 1.4
  step 7, 4.4). A new limit is a new row with a higher version.

clock_state stays with Part 4: the clock is still the real-time stub (app/clock.py).

The database roles are created by tests/sql/bootstrap_roles.sql until Part 4 lands. The
application role (the user in DATABASE_URL) gets normal read and write on all four tables
(spec 4.4, Pinned: everything except the ledger). The application code never writes api_key or
threshold_version; Part 4's seed and reset do, as the owner (decision 127).

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
TABLES = ("monitored_system", "sweeper_heartbeat", "api_key", "threshold_version")
ROLES = ("SYSTEM", "REVIEWER", "VIEWER")
COMPARATORS = ("gt", "lt")


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    return role


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


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
    op.create_table(
        "api_key",
        sa.Column("key_hash", sa.Text, primary_key=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("system_id", sa.Text, nullable=True),
        sa.CheckConstraint(f"role IN ({_in(ROLES)})", name="ck_api_key_role"),
        sa.CheckConstraint(
            "(role = 'SYSTEM') = (system_id IS NOT NULL)", name="ck_api_key_system_binding"
        ),
    )
    op.create_table(
        "threshold_version",
        sa.Column("threshold_version_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=False),
        sa.Column("metric_type", sa.Text, nullable=False),
        sa.Column("comparator", sa.Text, nullable=False),
        sa.Column("limit_value", sa.Numeric, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "metric_type IN ('performance', 'drift')", name="ck_threshold_version_metric_type"
        ),
        sa.CheckConstraint(
            f"comparator IN ({_in(COMPARATORS)})", name="ck_threshold_version_comparator"
        ),
        sa.UniqueConstraint(
            "system_id", "metric_name", "version", name="uq_threshold_version_version"
        ),
    )
    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    # IF EXISTS: a database upgraded by an earlier version of this stub may lack them.
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
