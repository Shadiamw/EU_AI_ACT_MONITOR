"""STUB for Part 1: Detection (spec 4.4, migration 0003).

Part 1 owns migration 0003 (metric_sample, breach_series, event, harm_field and their indexes).
Until Part 1's real 0003 replaces this file, it creates only the two tables Parts 2 and 3 use,
with the spec 1.7 table, column and index names, so Part 3's SQL works unchanged on the real
tables:

- event: every flagged event. The gap sweeper inserts monitoring_gap events (spec 1.7: "Part 3
  inserts gap events into your event table") and closes them.
- harm_field: four rows per event (spec 1.5 and 1.7).

metric_sample and breach_series are not created here; nothing in Parts 2 and 3 uses them.
The application role gets normal read and write (spec 4.4).

Revision ID: 0003
Revises: 0002
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = ("event", "harm_field")
EVENT_STATES = ("FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED")
HARM_FIELD_STATUSES = ("PREFILLED_PENDING_CONFIRMATION", "CONFIRMED", "NOT_APPLICABLE")


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    return role


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    app_role = _app_role()
    timestamp = sa.DateTime(timezone=True)

    op.create_table(
        "event",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=True),
        sa.Column("observed_value", sa.Numeric, nullable=True),
        sa.Column("threshold_value", sa.Numeric, nullable=True),
        sa.Column("threshold_version_id", sa.Text, nullable=True),
        sa.Column("detected_at", timestamp, nullable=False),
        sa.Column("received_at", timestamp, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("deadline_utc", timestamp, nullable=True),
        sa.Column("evidence_bundle", JSONB, nullable=True),
        sa.Column("evidence_sha256", sa.Text, nullable=True),
        sa.Column("retain_until", timestamp, nullable=False),
        sa.CheckConstraint(f"state IN ({_in(EVENT_STATES)})", name="ck_event_state"),
    )
    op.create_index(
        "ix_event_detected_at_event_id",
        "event",
        [sa.text("detected_at DESC"), "event_id"],
    )
    op.create_index(
        "uq_event_one_open_gap_per_system",
        "event",
        ["system_id"],
        unique=True,
        postgresql_where=sa.text("type = 'monitoring_gap' AND state = 'FLAGGED'"),
    )

    op.create_table(
        "harm_field",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("field_key", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("evidence", sa.Text, nullable=True),
        sa.Column("confirmed_by", sa.Text, nullable=True),
        sa.Column("confirmed_at", timestamp, nullable=True),
        sa.CheckConstraint(f"status IN ({_in(HARM_FIELD_STATUSES)})", name="ck_harm_field_status"),
    )

    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
